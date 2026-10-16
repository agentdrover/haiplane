"""Явные исходы доставки, не требующие чтения одобрения (#1706).

Вынесено из orchestration.py: файл на потолке бюджета размера. Обе записи —
исходы «форж не вызывался»; их пишет только ``record_delivery_outcome``.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo


async def note_delivery_unpinned(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> None:
    """Явный исход: merge не привязан к одобренной голове, форж не вызывался (#1706)."""
    text = (
        f"Доставка PR #{task.get('pr_number')} не выполнена: у одобренной сдачи не "
        "закреплён коммит, а хаб вливает PR только на ту голову, которая была "
        "одобрена (gh pr merge --match-head-commit). Форж не вызывался. Пересдайте "
        "работу, чтобы коммит закрепился, или решите по задаче вручную; хаб к этой "
        "доставке сам не вернётся."
    )
    updates = await repo.get_task_updates(db, int(task["id"]))
    if updates and (dict(updates[-1]).get("content") or "") == text:
        return
    await repo.add_task_update(db, int(task["id"]), "hub", "alert", text)
    await db.commit()


async def note_delivery_head_moved(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> None:
    """Явный исход: голова, на которой смотрели CI, ушла; форж не вызывался (#1706)."""
    text = (
        f"Доставка PR #{task.get('pr_number')} не выполнена: пока шли проверки "
        "(CI, стопка, свежесть базы), закрепление одобренной сдачи ушло на другую "
        "голову, и зелёный CI этого прохода относится не к ней. Форж не вызывался. "
        "Проверки нужно пройти заново на текущей голове."
    )
    updates = await repo.get_task_updates(db, int(task["id"]))
    if updates and (dict(updates[-1]).get("content") or "") == text:
        return
    await repo.add_task_update(db, int(task["id"]), "hub", "alert", text)
    await db.commit()


async def note_approval_gone(
    db: aiosqlite.Connection, task: dict[str, Any], *, merged: bool = False
) -> None:
    """Сказать один раз, что одобрения больше нет, и не обещать вернуться.

    ``merged`` — PR уже влит (мерж записан как факт): задача при этом не
    завершается, потому что завершение утверждает «одобренная работа доставлена».
    """
    if merged:
        text = (
            f"PR #{task.get('pr_number')} влит, но одобрение, с которым началась "
            "доставка, к этому моменту уже не действует (вердикт заменён, работа "
            "возвращена в доработку или пересдана): задача НЕ завершена. Влитый PR "
            "остаётся в базовой ветке; что дальше, решает человек по текущему "
            "вердикту на карточке."
        )
    else:
        text = (
            f"Доставка PR #{task.get('pr_number')} не выполнена: одобрение, с "
            "которым она началась, больше не действует (вердикт заменён, работа "
            "возвращена в доработку или пересдана). Что дальше, определяет "
            "текущий вердикт на карточке; хаб к этой доставке сам не вернётся."
        )
    updates = await repo.get_task_updates(db, int(task["id"]))
    if updates and (dict(updates[-1]).get("content") or "") == text:
        return
    await repo.add_task_update(db, int(task["id"]), "hub", "status", text)
    await db.commit()
