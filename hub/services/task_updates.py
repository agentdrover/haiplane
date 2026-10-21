"""Этапы ``lifecycle.add_update`` (#1690): запись апдейта задачи и маршруты done-отчёта.

Вынесено без изменения поведения: у ``add_update`` не было запаса сложности, а
у ``lifecycle.py`` — запаса размера. Имена, которые тесты подменяют через
``lifecycle`` (``prepare_ci_standing``, ``maybe_rollup_parent``,
``_finish_deferred_done``, ``log_activity``), здесь берутся из модуля
``lifecycle`` в момент вызова, а не копируются.

Инварианты, которые нельзя ослаблять:

* ``prepare_ci_standing`` (сеть) — до транзакции; внутри транзакции задача
  перечитывается (``_task_inside_transaction``), а гейт done решается заново
  (``_done_validation_gate`` → ``ci_trust.recheck_in_transaction``);
* владелец транзакции — ``apply_update``: ``defer_completions``,
  ``write_transaction``, SAVEPOINT ``done_flow``, ``except BaseException`` с
  откатом и RELEASE. В вынесенных этапах новых ``commit`` нет: существующий
  ``log_activity(commit=True)`` завершает транзакцию там же, где и раньше;
* deferred-доставка идёт после коммита, её отказ не откатывает done-строку.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import write_transaction
from hub.hub_instance import mutation_activity_detail
from hub.models import TaskUpdateCreate, TaskUpdateView
from hub.services import critic_orders
from hub.services import lifecycle as lc
from hub.services.done_hold import hold_done_report
from hub.services.orchestration import defer_completions
from hub.services.resubmission_guard import stop_empty_done
from hub.services import prevention_gate

log = logging.getLogger("hub")


async def normalize_done_report(
    db: aiosqlite.Connection, task: dict[str, Any], body: TaskUpdateCreate
) -> TaskUpdateCreate:
    """Что делать с done-отчётом до записи: принять как есть, переписать или отказать."""
    if body.kind != "done":
        return body
    # #971: the hub can now deliver approved pair work on its own, so an
    # agent that went away and came back may find its task already
    # complete. Refusing its report with "task_already_terminal" would be
    # true and useless: nothing is wrong, the work shipped, and the only
    # thing still missing is the account of it. The report is taken as
    # what it now is — a record — and the terminal guard stays exactly as
    # strict for every other way a task can end.
    if task.get("status") == "completed" and await repo.completed_by_poller_delivery(
        db, int(task["id"])
    ):
        return body.model_copy(update={"kind": "status"})
    # #1647: done-отчёт state-задачи — не сдача и не завершение.
    # До вставки строки: отказ не оставляет ни done-строки, ни bump.
    from hub.services.state_task import refuse_done_report

    refuse_done_report(task)
    lc._validate_done_report(task)
    return body


async def undelivered_advisory(
    db: aiosqlite.Connection, task: dict[str, Any], body: TaskUpdateCreate
) -> str:
    """#498: work that never started its way out, named while the report is written.

    Advisory by design: the completion is not blocked, and anything unknown
    stays silent.
    """
    if body.kind != "done":
        return ""
    from hub.services.delivery_gate import undelivered_warning

    try:
        return await undelivered_warning(db, task)
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        log.warning("delivery check for #%s failed: %s", task["id"], exc)
        return ""


async def _write_row_and_prevention(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    body: TaskUpdateCreate,
    principal_id: int | None,
) -> int:
    """The update row, then #919 prevention — inside the savepoint, before any route."""
    update_id = await repo.add_task_update(
        db,
        int(task["id"]),
        body.agent,
        body.kind,
        body.content,
        principal_id=principal_id,
        # "anonymous" is not "hub": an open-mode request really has no
        # identity, while the hub writing its own alerts has none by
        # nature. Telling them apart is the point of the field.
        author_kind="principal" if principal_id is not None else "anonymous",
    )
    # #919: a refused close of a prod defect takes its done row back with it.
    await prevention_gate.record_done_prevention(
        db,
        task,
        kind=body.kind,
        prevention=body.prevention,
        actor=body.agent or task.get("assigned_agent") or "agent",
    )
    return update_id


async def _record_finding_outcomes(
    db: aiosqlite.Connection, task: dict[str, Any], body: TaskUpdateCreate
) -> None:
    """#1155: ответ автора про находки предыдущей сдачи — до развилки и до бампа.

    До развилки — потому что done-отчёт уходит тремя дорогами (pending_report →
    review, pair running → transition_after_agent_done, поллер), и поставить
    запись в одну значило бы потерять ответ на двух. До бампа — потому что
    исходы принадлежат поколению, на которое ОТВЕЧАЮТ (#911). Внутри
    савпоинта: отказ разбора (422) откатывает и строку отчёта, и исходы.
    Условие на ПОЛЕ, а не на kind: отчёт завершённой задачи переписан в
    kind='status' (#971), и ответ автора не должен исчезать вместе с этим.
    """
    if not body.finding_outcomes:
        return
    outcome_note = await lc.record_done_report_outcomes(
        db,
        task,
        body.finding_outcomes,
        reported_by=(body.agent or task.get("assigned_agent") or ""),
    )
    if outcome_note:
        await repo.add_task_update(db, int(task["id"]), "hub", "status", outcome_note)


async def _route_pending_report(
    db: aiosqlite.Connection, task: dict[str, Any], body: TaskUpdateCreate
) -> None:
    """done на pending_report: в review (Universal Review Gate) или сразу completed."""
    task_id = int(task["id"])
    # #1783: и этот вход — сдача; пустую пересдачу после «на доработку» не пускаем.
    if await stop_empty_done(db, task):
        return
    # #1155: на ЭТОМ маршруте конвейер гейтов не доезжает — обе ветки уходят из
    # pending_report сами, не зовя transition_after_agent_done. Сказать о
    # находках, на которые отчёт не ответил, больше некому. Перед развилкой, а
    # не в ветке review: маршрут «завершить без ревью» уносит их ещё дальше.
    still_open_note = await lc.unanswered_findings_note(db, task)
    if still_open_note:
        await repo.add_task_update(db, task_id, "hub", "alert", still_open_note)
    if lc.completion_requires_review(task):
        # Universal Review Gate (#306): even the pending_report path may not
        # complete unreviewed work — the done report becomes a submission.
        generation = await repo.bump_submission_generation(db, task_id)
        # #1589: и этот вход — сдача; без гейтов конвейера он иначе остался бы
        # без предупреждений по путям.
        await lc._record_path_notices_for_done(db, task)
        # #1953: статус и проверенный sha - одной записью, до коммитящих вызовов.
        await critic_orders.accept_in_transaction(
            db, task, "review", review_job_id=None
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Universal Review Gate: done report routed to review "
            f"(submission #{generation}). Obtain a review "
            "(agent: a review report via hub_submit_machine_review or "
            "changes_requested via hub_submit_review; APPROVED comes from a "
            "human or the hub policy), then report done again.",
        )
        await lc.log_activity(
            db,
            "task_review_required",
            f"Task #{task_id} → review (gate) on report from {body.agent}",
            detail=mutation_activity_detail(),
        )
        return
    await critic_orders.accept_in_transaction(db, task, "completed")  # #1953
    await lc.log_activity(
        db,
        "task_completed",
        f"Task #{task_id} completed with report from {body.agent}",
        detail=mutation_activity_detail(),
    )
    await lc.maybe_rollup_parent(db, task_id)


async def _route_pair_or_claimed(
    db: aiosqlite.Connection, task: dict[str, Any], agent: str = ""
) -> None:
    """done на running/claimed без job: общий post-done переход, блокер, очистка claim.

    A done report on a pair-running task OR on a reserved (claimed) task must
    never be silently dropped. A claimed task never pair-started, so it has no
    branch — transition_after_agent_done then routes it to completed.
    """
    task_id = int(task["id"])
    # Захват снимает отчёт его держателя; чужой done (старая сессия, другой агент)
    # чужой захват не трогает. Захват прогона хаба (executor_run_id) отчёт не
    # снимает вовсе: его, вместе с сессиями и слотом, снимает закрытие прогона
    # (#1783), иначе доказательство ушло бы раньше, чем прогон закроется.
    holder = (task.get("claimed_by") or "").strip()
    proven = task.get("executor_run_id") is not None
    named = not agent or holder in ("", agent)  # без имени в отчёте — как на develop
    was_claimed = task["status"] == "claimed" and named and not proven
    updates_list = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    # #948: значим блокер, записанный ПОСЛЕ последней сдачи, а не любой в
    # истории. Снятое препятствие больше не уводит сдачу мимо доставки, а
    # живое — уводит, как и уводило.
    holding = lc.blocker_holding_the_done_report(updates_list)
    if holding is not None:
        await hold_done_report(db, task_id, lc.blocker_note(holding))
    else:
        await lc.transition_after_agent_done(db, task, has_done=True)
    if was_claimed:
        await repo.update_task(db, task_id, claimed_by=None, claim_session_id=None)
    refreshed = await repo.get_task(db, task_id)
    if refreshed and dict(refreshed)["status"] == "completed":
        await lc.maybe_rollup_parent(db, task_id)


async def _route_done(
    db: aiosqlite.Connection, task: dict[str, Any], body: TaskUpdateCreate
) -> None:
    """Маршрут апдейта после записи строки; не-done только трогает updated_at."""
    if body.kind != "done":
        await repo.update_task(db, int(task["id"]))
        return
    # Verifiable SDD (#510): under 'require', a done report that would complete
    # the task is blocked while the current validation run is red/absent. Only
    # completion-bound reports are gated; the poller path is not affected.
    await lc._done_validation_gate(db, task)
    if body.finding_outcomes:  # ответ на находки — не пустая пересдача (#1783)
        task = {**task, "_answered_findings": True}
    if task["status"] == "pending_report":
        await _route_pending_report(db, task, body)
    elif task["status"] in ("running", "claimed") and not task.get("job_id"):
        await _route_pair_or_claimed(db, task, (body.agent or "").strip())
        row = await repo.get_task(db, int(task["id"]))
        if row is not None and dict(row)["status"] in ("running", "claimed"):
            # #1953: завершение без ревью отложено за коммит; sha критика
            # закрепляется ЗДЕСЬ единой функцией приёма, после проверок маршрута
            # (пустая пересдача, блокер) и до коммита done-flow.
            if not lc.completion_requires_review(task):
                await critic_orders.accept_in_transaction(db, task, pin=True)
    # open + done without pair-start: rejected before insert (AC-2)


async def _inside_savepoint(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    body: TaskUpdateCreate,
    principal_id: int | None,
) -> int:
    """Everything the savepoint protects; the ONE ``commit`` here is the original one."""
    if body.kind == "done":
        # #1953: гейт критика - под write-локом: политику и заказы читаем здесь,
        # отказ откатывает савпоинт до строки отчёта.
        task = await critic_orders.accept_in_transaction(db, task) or task
    update_id = await _write_row_and_prevention(db, task, body, principal_id)
    await _record_finding_outcomes(db, task, body)
    await _route_done(db, task, body)
    await db.commit()
    await lc.log_activity(
        db,
        "task_update",
        f"Task #{task['id']} update from {body.agent}: {body.content[:80]}",
        detail=mutation_activity_detail(),
    )
    return update_id


async def apply_update(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    body: TaskUpdateCreate,
    principal_id: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Владелец транзакции: (перечитанная task, deferred_done, update_id).

    Serialize the whole mutation (insert + status transition + commits) on the
    shared connection so it cannot interleave with a refinement ``_atomic``
    SAVEPOINT (see get_write_lock). Nothing inside acquires the lock again.
    ``log_activity`` commits inside here, which is why it runs under the same
    lock.
    """
    async with defer_completions() as deferred_done, write_transaction(db):
        # #1687: nothing is gated on a dict prepared before the lock: a POST may
        # have withdrawn the CI evidence since. The task is read again HERE.
        task = await lc._task_inside_transaction(db, task)
        # #364: the done row and the generation bump are written before the git
        # tail runs, and a git adapter that raises used to leave both behind.
        # git itself cannot be rolled back, but the database can. No commit may
        # run between the insert and the git steps on this path; a commit would
        # have released the savepoint and defeated the rollback silently.
        await db.execute("SAVEPOINT done_flow")
        released = False
        try:
            update_id = await _inside_savepoint(db, task, body, principal_id)
        except BaseException:
            # Roll back to before the done row. RELEASE after ROLLBACK TO is
            # required: rolling back to a savepoint does not remove it (#364).
            await lc._release_done_flow(db, rollback=True)
            released = True
            raise
        finally:
            if not released:
                await lc._release_done_flow(db)
    return task, deferred_done, update_id


async def _undelivered_still_true(
    db: aiosqlite.Connection, task_id: int, undelivered: str
) -> str:
    """#967: the snapshot predates the transition, and the transition can ANSWER it.

    The hub opens the PR itself (and may merge it within the same report), and
    a refusal writes its own, louder needs_decision alert. "Хаб не знает ни PR"
    over a freshly recorded PR is the lie that teaches readers to skip the
    warning. The review-routing path is untouched: there the snapshot is true.
    """
    if not undelivered:
        return ""
    fresh_row = await repo.get_task(db, task_id)
    fresh = dict(fresh_row) if fresh_row is not None else {}
    if fresh.get("pr_number") or fresh.get("status") == "needs_decision":
        return ""
    return undelivered


async def finish_update(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskUpdateCreate,
    deferred_done: list[dict[str, Any]],
    undelivered: str,
) -> str:
    """After the commit: deferred delivery, workspace, completion note; the final advisory."""
    await lc._finish_deferred_done(db, task_id, deferred_done)
    if body.kind == "done":
        await lc._try_restore_pair_workspace(db, task_id)
        # #897, the third door: a ``pending_report`` task with auto_review off
        # completes right here, without the delivery gate ever running. The
        # gate's own path arrives here too and answers "delivered" for free, so
        # this is one call for both.
        after = await repo.get_task(db, task_id)
        if after is not None and dict(after)["status"] == "completed":
            await lc.note_completion_without_delivery(
                db, task_id, via="report_done", actor=body.agent or "agent"
            )
    undelivered = await _undelivered_still_true(db, task_id, undelivered)
    if undelivered:
        await repo.add_task_update(db, task_id, "hub", "alert", undelivered)
        await db.commit()
    return undelivered


async def view_of_update(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    body: TaskUpdateCreate,
    update_id: int,
    undelivered: str,
) -> TaskUpdateView:
    """The stored row as the API answers, with the report's warnings."""
    update_row = await repo.get_task_update_by_id(db, update_id)
    view = TaskUpdateView(**dict(update_row))  # type: ignore[arg-type]
    view.warnings = await lc._done_report_warnings(db, task, body.kind, undelivered)
    return view
