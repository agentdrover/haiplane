"""Отметка поведенческой дельты задачи: три состояния (#2217).

``unspecified`` (автор ничего не сказал), ``delta`` (дельта есть) и
``no_delta`` (автор явно заявил «поведение не меняется»). Пустое поле остаётся
``unspecified`` и ни при каком пути не превращается в ``no_delta``: иначе
пустая карточка читалась бы как сознательное решение и chore молча обходил бы
архив дельты. Форма записи дельты - задача #2218 и здесь не затрагивается.

Тесты идут через публичную дверь: REST-уточнение и чтение карточки.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hub.models import BehaviorDeltaMark, TaskRefine


async def _create_task(client: AsyncClient, **overrides) -> dict:
    resp = await client.post("/api/tasks", json={"title": "t", **overrides})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _refine(client: AsyncClient, task_id: int, **fields) -> dict:
    resp = await client.post(f"/api/tasks/{task_id}/refine", json=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _read(client: AsyncClient, task_id: int) -> dict:
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored(db, task_id: int) -> str:
    """Значение КАК ХРАНИТСЯ, а не как отдаёт модель ответа."""
    rows = await db.execute_fetchall(
        "SELECT behavior_delta FROM tasks WHERE id=?", (task_id,)
    )
    return dict(rows[0])["behavior_delta"]


async def test_omitted_mark_stays_unspecified(client: AsyncClient, db):
    """AC-1: поле не передано, задачу уточняют - значение остаётся «не сказано»."""
    task = await _create_task(client)
    assert task["behavior_delta"] == "unspecified"

    refined = await _refine(client, task["id"], problem_statement="ps", size="M")

    assert refined["behavior_delta"] == "unspecified"
    assert (await _read(client, task["id"]))["behavior_delta"] == "unspecified"
    assert await _stored(db, task["id"]) == "unspecified"


async def test_explicit_no_delta_is_distinct(client: AsyncClient, db):
    """AC-2: явное «поведенческой дельты нет» читается иначе, чем «не сказано»."""
    task = await _create_task(client)
    before = (await _read(client, task["id"]))["behavior_delta"]

    refined = await _refine(client, task["id"], behavior_delta="no_delta")

    after = (await _read(client, task["id"]))["behavior_delta"]
    assert refined["behavior_delta"] == "no_delta"
    assert after == "no_delta"
    assert after != before == "unspecified"
    assert await _stored(db, task["id"]) == "no_delta"


async def test_a_later_refine_without_the_field_keeps_the_mark(client: AsyncClient):
    task = await _create_task(client)
    await _refine(client, task["id"], behavior_delta="no_delta")

    refined = await _refine(client, task["id"], problem_statement="other edit")

    assert refined["behavior_delta"] == "no_delta"


@pytest.mark.parametrize("empty", [None, ""])
async def test_an_empty_value_is_unspecified_never_no_delta(
    client: AsyncClient, db, empty
):
    """Пустое значение - «не сказано»: ни на новой задаче, ни поверх явной отметки."""
    task = await _create_task(client)
    fresh = await _refine(client, task["id"], behavior_delta=empty)
    assert fresh["behavior_delta"] == "unspecified"

    await _refine(client, task["id"], behavior_delta="no_delta")
    cleared = await _refine(client, task["id"], behavior_delta=empty)

    assert cleared["behavior_delta"] == "unspecified"
    assert await _stored(db, task["id"]) == "unspecified"


async def test_delta_present_is_a_third_distinct_state(client: AsyncClient):
    task = await _create_task(client)

    refined = await _refine(client, task["id"], behavior_delta="delta")

    assert refined["behavior_delta"] == "delta"
    assert {m.value for m in BehaviorDeltaMark} == {"unspecified", "delta", "no_delta"}


async def test_an_unknown_value_is_refused_not_coerced(client: AsyncClient, db):
    task = await _create_task(client)

    resp = await client.post(
        f"/api/tasks/{task['id']}/refine", json={"behavior_delta": "none"}
    )

    assert resp.status_code == 422, resp.text
    assert await _stored(db, task["id"]) == "unspecified"


async def test_bulk_refine_writes_the_mark_per_item(client: AsyncClient):
    marked = await _create_task(client)
    silent = await _create_task(client)

    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {"task_id": marked["id"], "behavior_delta": "no_delta"},
                {"task_id": silent["id"], "problem_statement": "ps"},
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert (await _read(client, marked["id"]))["behavior_delta"] == "no_delta"
    assert (await _read(client, silent["id"]))["behavior_delta"] == "unspecified"


async def test_the_column_defaults_to_unspecified_for_existing_rows(db):
    """Миграция: строка без отметки читается «не сказано», а не пустотой или no_delta."""
    cols = {r[1]: r for r in await db.execute_fetchall("PRAGMA table_info(tasks)")}
    column = cols["behavior_delta"]
    assert column[2].upper() == "TEXT"
    assert column[3] == 1, "NOT NULL: пустота не бывает четвёртым состоянием"
    assert column[4] == "'unspecified'"


def test_omitted_field_is_not_set_on_the_refine_model():
    """Пропуск не равен значению: ``model_fields_set`` не видит непереданное поле."""
    assert "behavior_delta" not in TaskRefine().model_fields_set
    assert TaskRefine(behavior_delta="no_delta").behavior_delta == (
        BehaviorDeltaMark.no_delta
    )
    assert TaskRefine(behavior_delta=None).behavior_delta == (
        BehaviorDeltaMark.unspecified
    )
    assert TaskRefine(behavior_delta="").behavior_delta == (
        BehaviorDeltaMark.unspecified
    )
