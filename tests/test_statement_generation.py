"""Поколение постановки: единица счёта для суждений о драфте (#1156).

Задача существует против одного конкретного ограничения: суждения и
прогоны стюарда стоят на ``(task_id, generation, kind)``, а у драфта
поколение сдачи навсегда ноль. Поэтому тесты ниже проверяют не «работает
ли счётчик», а разные способы сделать его бесполезным — растущий всегда,
не растущий никогда, растущий так, что at-most-once из #1022 перестаёт
держать, и слепой к одному из путей записи. Последний способ и сработал:
первая редакция видела одиночный refine и не видела массовый.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.models import AcceptanceCriterion, BulkRefine, TaskRefine
from hub.services.refinement import (
    refine_task,
    refine_tasks_bulk,
    upsert_acceptance_criterion,
)
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


async def test_a_bulk_edit_moves_the_counter_too(db: aiosqlite.Connection):
    """AC-1 через ВТОРОЙ путь записи: массовый refine (находка ревью #232).

    Первая редакция повесила счётчик на ``recalc_readiness_inline`` и
    потеряла ``/refine-bulk``: тот считает готовность пакетом ПОСЛЕ коммита,
    вне ``_atomic``, и в эту функцию не заходит. Наблюдалось так: тот же
    патч через одиночный refine давал 1, через массовый — 0 при записанном
    тексте. Слот kind='dor' на (task_id, 0) при этом оставался занят, то
    есть цикл «стюард вернул → автор поправил пакетом → стюард читает
    снова» упирался в индекс именно на самом дешёвом для автора пути.

    Тест берёт ДВЕ задачи в одном пакете: правило, применённое к первому
    элементу и потерянное на остальных, — отдельный способ ошибиться, и
    одна задача его бы не показала.
    """
    first = await _draft(db, "первый драфт")
    second = await _draft(db, "второй драфт")
    before_first = await _generation(db, first)
    before_second = await _generation(db, second)

    await refine_tasks_bulk(
        db,
        BulkRefine.model_validate(
            {
                "items": [
                    {"task_id": first, "problem_statement": "что сломано у первого"},
                    {"task_id": second, "problem_statement": "что сломано у второго"},
                ]
            }
        ),
    )

    assert await _generation(db, first) == before_first + 1
    assert await _generation(db, second) == before_second + 1, (
        "второй элемент пакета — не бесплатное приложение к первому"
    )
    assert await _submission_generation(db, first) == 0, "поколение сдачи не задето"


async def test_a_bulk_rewrite_without_changes_does_not(db: aiosqlite.Connection):
    """AC-2 через массовый путь: пересохранение пакетом — не ревизия.

    Без этой половины находку можно было бы «закрыть» бампом на каждый
    заход в массовый refine: AC-1 позеленел бы, а признак «постановку
    правили» перестал бы что-либо значить именно там, где пакетом гоняют
    десятки задач разом.
    """
    task_id = await _draft(db)
    patch = {"items": [{"task_id": task_id, "scope_in": ["первый кусок"]}]}
    await refine_tasks_bulk(db, BulkRefine.model_validate(patch))
    settled = await _generation(db, task_id)

    await refine_tasks_bulk(db, BulkRefine.model_validate(patch))
    await refine_tasks_bulk(db, BulkRefine.model_validate(patch))

    assert await _generation(db, task_id) == settled, (
        "тот же пакет с теми же значениями — не новая ревизия"
    )


async def test_reading_readiness_is_not_a_statement_edit(db: aiosqlite.Connection):
    """Чтение готовности счётчик не двигает.

    ``get_readiness`` чинит устаревшие сохранённые значения по ходу чтения и
    пишет в строку задачи — то есть чтение карточки доходит до записи. Это
    держится отпечатком, а не местом вызова: перенос бампа в саму запись
    полей готовности этот тест НЕ уронит, потому что чтение постановку не
    меняет и отпечаток совпадёт. Проверяется здесь именно гарантия
    «просмотр не покупает стюарду прогон», а ломает её реализация «расти на
    каждом вызове» — на ней тест краснеет.
    """
    from hub.services.refinement import _persisted_readiness_stale, get_readiness
    from hub.services.recommendations import calculate_readiness_with_recommendations

    task_id = await _draft(db)
    await refine_task(db, task_id, TaskRefine(problem_statement="что именно сломано"))
    settled = await _generation(db, task_id)

    # Ленивая починка срабатывает только на РАСХОЖДЕНИИ сохранённого счёта с
    # пересчитанным. Без этой строки тест зелёный при любой реализации: он
    # просто не доходит до записи. Счёт правится в обход воронки — именно
    # так и появляются устаревшие значения, ради которых починка написана.
    await repo.update_task(db, task_id, readiness_score=1)
    await db.commit()
    row = await repo.get_task(db, task_id)
    report = await calculate_readiness_with_recommendations(db, task_id)
    assert _persisted_readiness_stale(row, report), "предусловие: починка сработает"

    await get_readiness(db, task_id)
    await get_readiness(db, task_id, explain=True)

    assert await _generation(db, task_id) == settled


async def test_viewing_an_unbaselined_row_buys_no_revision(db: aiosqlite.Connection):
    """AC-1: просмотр карточки БЕЗ СНЯТОГО БАЗИСА ревизией не является.

    Отличие от соседнего теста — в одной строке, которой здесь нет: он
    начинается с ``refine_task``, то есть читает уже базированную строку, и
    перенос бампа в саму запись полей готовности его не уронит — отпечаток
    совпадёт, бампа не будет. Здесь отпечаток ПУСТ, а пустая строка не равна
    sha256 ни от чего, поэтому бамп на этом пути объявил бы ревизию — при
    том, что постановку никто не трогал.

    Цена ошибки — не косметика: ``get_readiness`` чинит устаревший счёт по
    ходу чтения и пишет в строку, так что обычный GET карточки дал бы
    поколение 0 → 1, а слот ``kind='dor'`` на этой ревизии оказался бы
    потрачен просмотром, а не правкой. Стюард, уже высказавшийся на нуле,
    потерял бы место для следующего суждения.
    """
    from hub.services.refinement import _persisted_readiness_stale, get_readiness
    from hub.services.recommendations import calculate_readiness_with_recommendations

    task_id = await _draft(db)
    row = await repo.get_task(db, task_id)
    assert not dict(row)["statement_fingerprint"], "предусловие: базис не снят"
    assert await _generation(db, task_id) == 0

    # Ленивая починка срабатывает только на РАСХОЖДЕНИИ сохранённого счёта с
    # пересчитанным. Без этих строк тест зелёный при любой реализации: он не
    # доходит до записи, а проверять нечего именно в записи.
    await repo.update_task(db, task_id, readiness_score=1)
    await db.commit()
    row = await repo.get_task(db, task_id)
    report = await calculate_readiness_with_recommendations(db, task_id)
    assert _persisted_readiness_stale(row, report), "предусловие: починка сработает"

    await get_readiness(db, task_id)

    row = await repo.get_task(db, task_id)
    assert dict(row)["readiness_score"] == report.score, (
        "предусловие проверено фактом: чтение действительно записало в строку"
    )
    assert await _generation(db, task_id) == 0, (
        "просмотр небазированной карточки не покупает стюарду прогон"
    )
    assert not dict(row)["statement_fingerprint"], (
        "и базиса чтение не снимает: отпечаток берут пути записи"
    )


async def test_a_row_without_a_baseline_declares_one_revision(
    db: aiosqlite.Connection,
):
    """Пустой отпечаток считается изменением — это решение, а не сентинел.

    У задачи, созданной до колонки или без снятого базиса, отпечаток пустой,
    и первый путь записи объявит ревизию даже при сохранении теми же
    значениями. Дешевле обратной ошибки: «пустой значит базис, поднять
    молча» оставило бы на нуле драфт, который стюард уже прочитал на нуле, и
    второе чтение после настоящей правки упёрлось бы в уникальный индекс.
    Лишний слот один раз на строку против оборванного цикла F6.

    Фиксируется здесь, чтобы смена стороны была видимым изменением
    контракта, а не молчаливым следствием сравнения с пустой строкой.
    """
    task_id = await _draft(db)
    row = await repo.get_task(db, task_id)
    assert not dict(row)["statement_fingerprint"], "предусловие: базис не снят"

    # Ничего не меняющий заход: значения совпадают с тем, что уже в строке.
    await refine_task(db, task_id, TaskRefine(title="драфт"))
    assert await _generation(db, task_id) == 1

    # А дальше правило обычное: базис снят, пересохранение не считается.
    await refine_task(db, task_id, TaskRefine(title="драфт"))
    assert await _generation(db, task_id) == 1


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
