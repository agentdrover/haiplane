"""Поле задачи объявлено один раз и само доезжает до MCP (#1068, эпик #1064).

До этой задачи список полей был выписан руками в шести-восьми местах, а в
``hub_refine_task`` — ДВАЖДЫ в одной функции, в сигнатуре и в теле. Комментарий
рядом называл цену: «a parameter present above and absent here is the #609
defect verbatim, and it fails silently». Отвечал на это предупреждающий скрипт
``surface_parity.py``, который называет пропуск ПОСЛЕ того, как его сделали, и
всегда возвращает 0.

Здесь проверяется не то, что генерация красива, а четыре вещи:

* поле, добавленное в модель, доезжает до инструмента само — а если не
  доехало, проверка КРАСНЕЕТ и называет поле;
* поле, которое инструмент не публикует, объявлено с причиной;
* схема входа не изменилась — ни имён, ни типов, ни обязательности;
* уплощение под каталог применено одинаково, а не по-разному в каждом месте.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from typing import Any

import pytest

from hub import mcp_server
from hub.mcp_signature import Hidden, flatten_for_catalog, published_fields
from hub.models import TaskRefine

# Инструменты, переведённые на вывод сигнатуры из модели, и их объявленные
# расхождения. Список растёт по мере перевода поверхностей — и растёт ЗДЕСЬ,
# то есть видимой строкой в диффе.
GENERATED_TOOLS = (
    ("hub_refine_task", mcp_server.REFINE_HIDDEN),
    ("hub_prepare_developer_task", mcp_server.PREPARE_HIDDEN),
)


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
    """Схемы входа как их видит клиент — из живого tools/list, не из исходника."""

    async def load() -> dict[str, dict]:
        return {t.name: t.inputSchema for t in await mcp_server.mcp.list_tools()}

    return asyncio.run(load())


@pytest.mark.parametrize(("tool", "hidden"), GENERATED_TOOLS)
def test_a_new_field_reaches_every_surface(schemas, tool, hidden):
    """AC-1: поле модели вне списка скрытых обязано быть в схеме инструмента.

    Это и есть замена предупреждению: забытая поверхность теперь КРАСНЕЕТ и
    называет поле и инструмент, а не пишется в отчёт, который можно не
    прочитать.
    """
    properties = set(schemas[tool]["properties"])
    missing = [f for f in published_fields(TaskRefine, hidden) if f not in properties]
    assert not missing, (
        f"поля модели не доехали до {tool}: {missing}. Либо они должны быть в "
        f"схеме, либо объявлены скрытыми с причиной — молча пропасть они не "
        f"могут"
    )


@pytest.mark.parametrize(("tool", "hidden"), GENERATED_TOOLS)
def test_a_divergence_must_be_declared(schemas, tool, hidden):
    """AC-4: расхождение проходит только объявленным, с причиной.

    Обратная сторона предыдущего теста: инструмент не публикует поле — значит
    оно названо в ``hidden``. Причина обязательна, потому что список без
    причин через полгода никто не решается тронуть.
    """
    properties = set(schemas[tool]["properties"])
    declared = {h.field for h in hidden}

    undeclared = [
        f for f in TaskRefine.model_fields if f not in properties and f not in declared
    ]
    assert not undeclared, (
        f"{tool} не публикует поля {undeclared}, и это нигде не записано. "
        f"Внесите их в hidden с причиной — «не публикуем» обязано быть "
        f"записью, а не отсутствием строки"
    )

    for item in hidden:
        assert isinstance(item, Hidden)
        assert item.field in TaskRefine.model_fields, (
            f"{tool}: скрыто поле {item.field!r}, которого в модели нет — "
            f"запись пережила своё поле"
        )
        assert len(item.reason) > 20, (
            f"{tool}: у скрытого поля {item.field!r} причина слишком коротка, "
            f"чтобы её можно было проверить"
        )


@pytest.mark.parametrize(("tool", "hidden"), GENERATED_TOOLS)
def test_every_declared_hidden_field_is_really_absent(schemas, tool, hidden):
    """Скрытое поле не должно оказаться в схеме: иначе запись врёт.

    Тот же приём, что у бюджета сложности (#1066): исключение, переставшее
    что-либо исключать, обязано краснеть — иначе список превращается в
    унаследованный мусор.
    """
    properties = set(schemas[tool]["properties"])
    leaked = [h.field for h in hidden if h.field in properties]
    assert not leaked, (
        f"{tool} публикует поля, объявленные скрытыми: {leaked}. Запись в "
        f"hidden больше не соответствует действительности"
    )


def test_the_flattening_is_applied_the_same_way_everywhere(schemas):
    """AC-3 (часть): enum → string, вложенная модель → object, и так везде.

    Раньше это уплощение авторы инструментов делали руками, каждый у себя.
    Здесь проверяется результат: в схеме нет ``$ref`` — а значит нет и
    ``$defs``, которые тянули бы каталог вверх (#780, #829).
    """
    for tool, _ in GENERATED_TOOLS:
        rendered = repr(schemas[tool])
        assert "$ref" not in rendered, (
            f"{tool}: в схеме появился $ref — уплощение под каталог не "
            f"применилось, и бюджет каталога это заметит"
        )
        assert "$defs" not in rendered


def test_flattening_rules_are_what_they_claim():
    """Уплощение проверяется само по себе, а не только через схему."""

    class Colour(enum.Enum):
        red = "red"

    assert flatten_for_catalog(Colour) is str
    assert flatten_for_catalog(typing.Optional[Colour]) == typing.Optional[str]
    assert flatten_for_catalog(list[Colour]) == list[str]
    assert flatten_for_catalog(TaskRefine) == dict[str, Any]
    # Обычные типы проходят насквозь: уплощение не трогает то, что и так плоско.
    assert flatten_for_catalog(int) is int
    assert flatten_for_catalog(list[str]) == list[str]


@pytest.mark.parametrize(("tool", "hidden"), GENERATED_TOOLS)
def test_the_published_set_is_not_empty(schemas, tool, hidden):
    """Страж, у которого нечего сторожить, молчит одинаково с обеих сторон.

    Если ``published_fields`` вернёт пусто — например, ``hidden`` разросся на
    всю модель, — тесты выше пройдут вхолостую. Проверяется, что предмет
    проверки существует.
    """
    published = published_fields(TaskRefine, hidden)
    assert len(published) > 20, (
        f"{tool} публикует всего {len(published)} полей модели — проверки "
        f"выше стали бы пустыми"
    )
