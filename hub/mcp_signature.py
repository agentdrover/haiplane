"""Сигнатура MCP-инструмента выводится из модели, а не переписывается (#1068).

ЗАЧЕМ. Список полей задачи выписан руками в шести-восьми местах. В
``hub_refine_task`` он написан ДВАЖДЫ в одной функции — в сигнатуре и в теле,
— и комментарий там называет вещи своими именами: «a parameter present above
and absent here is the #609 defect verbatim, and it fails silently». Поле,
добавленное в сигнатуру и забытое в теле, молча не доезжает до PATCH.
Consistency — самая плотная семья подтверждённых находок ревью в этом
репозитории (#810, #819, #833), и отвечал на неё до сих пор предупреждающий
скрипт, который называет пропуск ПОСЛЕ того, как его сделали.

ПОЧЕМУ ПРОЕКЦИЯ, А НЕ ПРЯМОЙ ВЫВОД. Замерено, а не предположено: сигнатура,
собранная из ``TaskRefine`` дословно, даёт ДРУГУЮ схему — 38 свойств вместо
34 и девять расхождений. Модель отдаёт ``$ref`` на enum и вложенные модели,
а инструмент их уплощает; плюс четыре поля модели инструмент не публикует
вовсе. То есть дословная генерация сменила бы публичный контракт: агенты
увидели бы другие типы и четыре новых параметра, а бюджет каталога (#780,
#829) пришлось бы пересчитывать.

Поэтому генерация идёт ЧЕРЕЗ ОБЪЯВЛЕННУЮ ПРОЕКЦИЮ: что скрыто и как
уплощается — данные в этом модуле, а не разбросанные решения в теле каждого
инструмента. Проверено: с проекцией схема совпадает со старой байт в байт,
34 из 34, тот же ``required``.

ЧТО ЭТО МЕНЯЕТ. Поле, добавленное в модель, доезжает до MCP само. Поле,
которое публиковать НЕ надо, приходится назвать в ``HIDDEN`` с причиной —
то есть «не публикуем» становится записью, а не отсутствием строки.
"""

from __future__ import annotations

import enum
import inspect
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Hidden:
    """Поле модели, которое MCP-инструмент намеренно НЕ публикует.

    Причина обязательна и хранится рядом с именем: список без причин за
    полгода превращается в набор строк, которые никто не решается тронуть,
    потому что неизвестно, чем они были.
    """

    field: str
    reason: str


def flatten_for_catalog(annotation: Any) -> Any:
    """Уплощение типа под каталог MCP: enum → str, вложенная модель → dict.

    Не косметика и не упрощение ради простоты. ``$ref`` тянет за собой
    ``$defs`` в схему каждого инструмента, а каталог — налог, который агент
    платит на каждом ходу (#780): потолок и запас под ним объявлены в
    docs/agent-context/mcp-catalog-budget.json. Уплощение — то, что авторы
    инструментов делали руками; здесь оно записано один раз и одинаково.

    Цена размена названа честно: агент не видит допустимых значений enum в
    схеме. Их несёт докстрока инструмента, и это осознанный обмен, а не
    недосмотр — ровно так эти параметры и были объявлены до #1068.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] и X | None: разворачиваем, уплощаем внутренность, собираем.
    if args and type(None) in args:
        inner = [a for a in args if a is not type(None)]
        if len(inner) == 1:
            return typing.Optional[flatten_for_catalog(inner[0])]
    if origin is list and args:
        # Подписка строится вызовом, а не литералом: mypy разбирает
        # ``list[X]`` статически и не принимает вычисленный аргумент, хотя в
        # рантайме он законен. Через __class_getitem__ то же самое, но без
        # статического разбора — и без подавления, которое пришлось бы
        # объяснять каждому читателю.
        return list.__class_getitem__(flatten_for_catalog(args[0]))
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return str
        if issubclass(annotation, BaseModel):
            return dict[str, Any]
    return annotation


def signature_from_model(
    model: type[BaseModel],
    *,
    leading: tuple[tuple[Any, ...], ...] = (),
    trailing: tuple[tuple[str, Any, Any], ...] = (),
    hidden: tuple[Hidden, ...] = (),
    returns: Any = str,
) -> tuple[inspect.Signature, dict[str, Any]]:
    """Сигнатура и аннотации инструмента по полям модели.

    ``leading`` — обязательные параметры перед полями модели (``task_id``).
    ``trailing`` — параметры инструмента, которых в модели нет вовсе: у них
    своя семантика (``mode``, ``analyst``), и притворяться, что они поля
    задачи, было бы враньём. ``hidden`` — поля модели, которые инструмент не
    публикует, с причиной у каждого.

    Все поля модели необязательны и по умолчанию ``None``: инструмент —
    PATCH, и «не передано» обязано отличаться от «передано пустым».
    """
    skip = {h.field for h in hidden}
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}

    for entry in leading:
        # Двойка — обязательный параметр, тройка — со значением по умолчанию.
        # Порядок параметров сохраняется дословно: он часть того, что видит
        # агент, и менять его «заодно» значило бы двигать контракт молча.
        name, ann = entry[0], entry[1]
        annotations[name] = ann
        extra: dict[str, Any] = {"annotation": ann}
        if len(entry) == 3:
            extra["default"] = entry[2]
        params.append(
            inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, **extra)
        )

    for name, field in model.model_fields.items():
        if name in skip:
            continue
        ann = flatten_for_catalog(field.annotation)
        annotations[name] = ann
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None,
                annotation=ann,
            )
        )

    for name, ann, default in trailing:
        annotations[name] = ann
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=ann,
            )
        )

    # Возврат приходит параметром, а не берётся str по умолчанию: у
    # hub_refine_task он HubRefineTaskResult, и подмена сменила бы схему
    # ВЫВОДА инструмента — контракт ровно так же, как и схема входа.
    # Поймано сверкой байт в байт, а не рассуждением.
    annotations["return"] = returns
    return inspect.Signature(params, return_annotation=returns), annotations


def published_fields(
    model: type[BaseModel], hidden: tuple[Hidden, ...] = ()
) -> tuple[str, ...]:
    """Поля модели, которые инструмент публикует. Для стража и отчёта."""
    skip = {h.field for h in hidden}
    return tuple(n for n in model.model_fields if n not in skip)


def with_model_signature(
    model: type[BaseModel],
    *,
    leading: tuple[tuple[str, Any] | tuple[str, Any, Any], ...] = (),
    trailing: tuple[tuple[str, Any, Any], ...] = (),
    hidden: tuple[Hidden, ...] = (),
    returns: Any = str,
):
    """Навесить на функцию сигнатуру, выведенную из модели.

    Функция объявляется как ``async def f(**kw)`` и получает поля именованными
    аргументами. FastMCP читает ``__signature__`` и ``__annotations__``, и
    отдаёт ту же схему, что была у рукописной сигнатуры.
    """

    def decorate(fn):
        sig, ann = signature_from_model(
            model,
            leading=leading,
            trailing=trailing,
            hidden=hidden,
            returns=returns,
        )
        fn.__signature__ = sig
        fn.__annotations__ = ann
        # Публикуемые и скрытые поля висят на функции: страж читает их
        # оттуда, а не повторяет вычисление и не расходится с ним.
        fn.model_fields_published = published_fields(model, hidden)
        fn.model_fields_hidden = hidden
        return fn

    return decorate
