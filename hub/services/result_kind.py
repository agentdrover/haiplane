"""Единый предикат «автоматика неприменима к задаче-состоянию» (#1647).

Задача с ``result_kind=state`` ничего не кладёт в репозиторий: у неё нет ветки,
PR и CI, а результат — наблюдение человека над миром. Поэтому всё, что в хабе
работает на коде, — машинное ревью, судья и советник стюарда, автовердикт,
автоодобрение, headless- и облачный исполнитель, сверка путей очереди — к ней
неприменимо. Правило ОДНО и живёт здесь: каждая дверь автоматики зовёт
``automation_not_applicable`` и не пишет собственное сравнение со строкой
``"state"``. Дверь, добавленная завтра, обязана позвать то же имя — иначе
state-задача окажется у неё без охраны, а искать такую дверь будет некому
(карта мест: ``.claude/state-task-call-site-map.md``, предикат закреплён
тестами ``test_no_automation_touches_state_tasks`` и соседями).

Модуль без зависимостей от остального хаба намеренно: его зовут и сервисы
стюарда, и очередь, и слой исполнителя, и импорт в обратную сторону дал бы
цикл.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RESULT_KIND_COMMIT = "commit"
RESULT_KIND_STATE = "state"

#: Каким AC может быть подтверждена state-задача: автоматические проверки
#: внешнего состояния с AC в MVP не связываются, поэтому ``test`` не годится
#: (он остаётся допустимым рядом, но сам по себе профиль не закрывает).
STATE_AC_KINDS: tuple[str, ...] = ("manual", "log_check", "ui_check")

#: Текст отказа, одинаковый во всех дверях: человек, прочитавший его в карточке
#: или в ответе API, узнаёт одно и то же правило, а не пять похожих.
AUTOMATION_REFUSAL = (
    "задача-состояние (result_kind=state): автоматика неприменима — у неё нет "
    "ветки, PR и CI, результат принимает человек по доказательствам сдачи"
)

#: Причина отказа для машинных читателей (события, строки заказов).
AUTOMATION_REFUSAL_CODE = "state_task_no_automation"


def result_kind_of(task: Mapping[str, Any] | Any | None) -> str:
    """Результат задачи; нет колонки или значения — ``commit``.

    Отсутствие читается как commit осознанно: так ведут себя все строки до
    миграции, и «не знаю» не должно выключать охрану commit-задач. Охрана
    state-задач от этого не страдает: у настоящей state-строки колонка есть.
    """
    if task is None:
        return RESULT_KIND_COMMIT
    try:
        value = task["result_kind"]
    except (KeyError, IndexError, TypeError):
        value = getattr(task, "result_kind", None)
    text = str(getattr(value, "value", value) or "").strip().lower()
    return text or RESULT_KIND_COMMIT


def is_state(task: Mapping[str, Any] | Any | None) -> bool:
    """Задача-состояние? Принимает строку БД, словарь и модель."""
    return result_kind_of(task) == RESULT_KIND_STATE


def automation_not_applicable(task: Mapping[str, Any] | Any | None) -> bool:
    """Единый предикат: автоматике эту задачу не отдают.

    Сегодня это ровно ``is_state``; отдельное имя нужно, чтобы двери
    автоматики читались как вопрос «можно ли автоматике», а не «какого вида
    задача» — и чтобы второй вид задач, которому автоматика не положена, был
    правкой одного места.
    """
    return is_state(task)


def qualifying_ac_count(ac_rows: Any) -> int:
    """Сколько AC годятся для state: verifiable_by из ``STATE_AC_KINDS``."""
    return sum(
        1
        for row in ac_rows
        if str(getattr(row["verifiable_by"], "value", row["verifiable_by"]))
        in STATE_AC_KINDS
    )


async def task_automation_not_applicable(db: Any, task_id: int) -> bool:
    """То же по номеру задачи: читает одну колонку, ничего не пишет."""
    from hub.db import fetchall

    rows = await fetchall(db, "SELECT result_kind FROM tasks WHERE id = ?", (task_id,))
    return bool(rows) and automation_not_applicable(rows[0])
