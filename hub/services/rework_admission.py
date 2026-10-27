"""Допуск круга починки running-задачи: одно правило для слота и облака (#1998).

После changes_requested задача стоит в running. Круг починки берёт её, только
когда за ней нет НИ ОДНОГО признака работы (#1783), были прогоны исполнителя,
проект в режиме manual и есть что чинить. Раньше это правило жило внутри
``executor_launch._admit_rework`` и знало только облако; слот (#1712) читал
``open`` и требовал ручного return-to-work. Теперь оба вызывают ``refusal`` —
проверку БЕЗ записи — и ``return_reason`` для записи о возврате. Сам возврат
(``return_running.apply_return_to_open``) вызывающий делает в своей транзакции
уже после всех отказов: облако — внутри брони, слот — под единым SAVEPOINT
вместе с выпуском кода и заказа.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.services import project_policy
from hub.services.result_kind import automation_not_applicable

#: Находки вердикта, которые идут в круг починки: вне области их чинит не этот круг.
_IN_SCOPE = "in_scope"


@asynccontextmanager
async def savepoint(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """SAVEPOINT вокруг допуска, возврата и брони: сбой откатывает ИХ записи.

    ``write_transaction`` во вложенном вызове ничего не откатывает: если внешний
    владелец транзакции перехватил ошибку и закоммитил, возврат в open, сброс
    цикла и бронь без кода остались бы (#1998). Как ``_issue_atomically`` слота.
    """
    await db.execute("SAVEPOINT rework_reserve")
    try:
        yield
    except BaseException:
        await db.execute("ROLLBACK TO rework_reserve")
        await db.execute("RELEASE rework_reserve")
        raise
    await db.execute("RELEASE rework_reserve")


def verdict_findings(task: dict[str, Any]) -> list[dict[str, str]]:
    """Находки ТЕКУЩЕГО вердикта changes_requested (человека или стюарда).

    Вердикт читается из строки задачи: он «текущий», пока покрывает нынешнюю
    сдачу (``review_verdict_covers_current_submission``). Нечитаемый JSON даёт
    пустой список, то есть отказ «нет находок», а не допуск.
    """
    from hub.services.lifecycle import parse_review_findings
    from hub.services.orchestration import review_verdict_covers_current_submission

    if task.get("review_verdict") != "changes_requested":
        return []
    if not review_verdict_covers_current_submission(task):
        return []
    return [
        {
            "uid": "",
            "kind": f"замечание вердикта №{f.id}, {f.severity.value}",
            "title": f.message,
            "where": f"{f.file}:{f.line}" if f.file and f.line else f.file,
            "text": f.recommendation,
        }
        for f in parse_review_findings(task.get("review_findings"))
        if f.scope.value == _IN_SCOPE
    ]


def verdict_note(findings: list[dict[str, str]]) -> list[str]:
    """Строка для промпта: у замечаний вердикта нет uid, их исходы — в summary сдачи.

    ``finding_outcomes`` принимает только адреса машинных находок; требовать от
    исполнителя невыполнимого значило бы получить отказ сдачи.
    """
    if all(f["uid"] for f in findings):
        return []
    return [
        "Замечания вердикта (без uid): исправь каждое и назови номер и исход в "
        "summary сдачи; в finding_outcomes их не передавай."
    ]


def verdict_unreadable(task: dict[str, Any]) -> bool:
    """Текущий вердикт changes_requested несёт находки, которые не читаются.

    Мягкий разбор отдаёт для них пустой список, и без этой проверки порча
    выглядела бы как «находок нет».
    """
    from hub.models import ReviewFinding
    from hub.services.orchestration import review_verdict_covers_current_submission

    raw = task.get("review_findings")
    if (
        task.get("review_verdict") != "changes_requested"
        or not raw
        or not review_verdict_covers_current_submission(task)
    ):
        return False
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        [ReviewFinding(**f) for f in data]
    except (ValueError, TypeError):
        return True
    return False


def findings_refusal(task: dict[str, Any], findings: list[dict[str, str]]) -> str:
    """Отказ круга по находкам: их нет, или замечания вердикта не читаются.

    Порча — отдельная причина, а не «нет находок»: мягкий разбор отдаёт для
    неё пустой список, и круг по одним машинным находкам молча пропустил бы
    замечания человека.
    """
    from hub.services.executor_launch import REASON_NO_FINDINGS

    if verdict_unreadable(task):
        return f"{REASON_NO_FINDINGS}: находки вердикта не читаются"
    return "" if findings else REASON_NO_FINDINGS


def return_reason(task: dict[str, Any], *, where: str, issue: str, who: str) -> str:
    """Причина записи о возврате: куда заказан круг, по какой сдаче, кто нажал."""
    return (
        f"круг починки {where} по находкам сдачи "
        f"{task.get('submission_generation')} ({issue}), нажал {who}"
    )


async def refusal(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """Почему running-задачу нельзя отдать кругу починки; пусто — можно.

    Зовётся ПОД write-локом вызывающего и перечитывает всё сама: ``task`` — уже
    свежая строка, а прогоны, признаки работы, политика и находки читаются здесь
    заново. Для ``open`` допуск безусловный (задачу вернули раньше, заказ
    сорвался). Ничего не пишет.
    """
    from hub.services import executor_launch as el
    from hub.services import return_running

    task_id = int(task["id"])
    if task["status"] == "open":
        return ""
    if task["status"] != "running":
        # Под локом задачу успели взять или сдвинуть: поверх чужого захвата
        # исполнитель не покупается.
        return f"{el.REASON_NOT_RETURNABLE}: статус {task['status']} под локом брони"
    if automation_not_applicable(task):
        return f"{el.REASON_STATE_TASK}: #{task_id}"
    not_leaf = await el._not_leaf_refusal(db, task)
    if not_leaf is not None:
        return not_leaf.reason
    policy = await project_policy.gate_policy_for_task(db, task_id)
    if el.launch_mode_of(policy) != el.LAUNCH_MANUAL:
        return el.REASON_OFF
    if not await repo.list_executor_runs(db, task_id):
        return el.REASON_NO_EXECUTOR_RUN
    live = await el._live_run(db, task_id)
    if live:
        return live
    signs = await return_running.work_signs(db, task)
    if signs:
        named = ", ".join(f"{s['sign']} ({s['detail']})" for s in signs)
        return f"{el.REASON_NOT_RETURNABLE}: у задачи есть признаки работы: {named}"
    return findings_refusal(task, await el._current_findings(db, task))
