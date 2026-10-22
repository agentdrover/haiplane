"""#1992: отметка «эскалация срока ожидания идёт» - взаимное исключение с мержем.

Мерж PR задачи и перевод её в needs_decision по сроку ожидания (``delivery_wait``)
не должны оба состояться по одной сдаче: форж подтвердил бы мерж, а задача
получила бы needs_decision и ложное событие. SQLite-лок этого не даёт - сосед
читает задачу и зовёт форж, не беря лока.

Замок вокруг гейта здесь не годится (см. комментарий у ``_gate_merges`` в
orchestration: report_done держит write-лок, ожидание на asyncio-замке роняло
доставку влитого PR). Поэтому отметка НЕ БЛОКИРУЕТ: множество (задача, PR) в
памяти процесса. Ставит её ``delivery_wait._escalate`` синхронно, без ``await``
после последней проверки памяти мержей; единственная точка мержа
(``merge_if_approval_stands``) читает её в своей секции без ``await`` и при
отметке отвечает отказом, форж не зовёт. Хаб - один процесс, поэтому памяти
процесса достаточно; модуль без импортов, чтобы не создавать циклов.
"""

from __future__ import annotations

_held: set[tuple[int, int]] = set()
_escalated: set[tuple[int, int, int]] = set()


def hold(key: tuple[int, int]) -> None:
    """Поставить отметку: перевод идёт, мерж этого PR не стартует."""
    _held.add(key)


def release(key: tuple[int, int]) -> None:
    """Снять отметку (после коммита статуса и события, либо при сбое)."""
    _held.discard(key)


def held(key: tuple[int, int]) -> bool:
    """Идёт ли перевод по этой задаче и PR."""
    return key in _held


def mark_escalated(key: tuple[int, int], generation: int) -> None:
    """Перевод по сроку состоялся и закоммичен: пока задача в needs_decision, не мержить.

    Прошедшие проходы автоматики (свип, отложенное завершение, headless) нельзя
    отличить по снимку: одни заменяют его свежим чтением строки. Поэтому память
    процесса держит факт перевода, а не вид снимка. Отметка живёт, пока живая
    строка в needs_decision; решение человека (decide accept ставит completed до
    доставки, rework возвращает в работу) снимает её при первом же взгляде.
    """
    _escalated.add((*key, generation))


def refuses(key: tuple[int, int], row: dict[str, object]) -> bool:
    """Мерж не стартует: перевод идёт, либо он состоялся и задача ещё у человека."""
    if held(key):
        return True
    mark = (*key, int(row.get("submission_generation") or 0))  # type: ignore[call-overload]
    if mark not in _escalated:
        return False
    if row.get("status") == "needs_decision":
        return True
    _escalated.discard(mark)  # человек решил: путь свободен
    return False
