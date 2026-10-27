"""Снятие исторических обещаний исхода решением владельца (#2091).

Журнал «проверка снята, результат неизвестен». Это НЕ ответ (#819): ответ
говорит, что измерили; снятие говорит, что измерять уже никто не будет, и
результат неизвестен. Поэтому:

* ``outcome_answers``, ``tasks.archived``, lifecycle и лента задач
  (``task_updates``) не затрагиваются: ~630 записей лент одним применением
  разбудили бы входящие, дайджест и стюарда, а аудит (identity, причина,
  batch, время) уже лежит в самой строке журнала;
* поздний ответ сильнее снятия: пока у задачи есть хоть один ответ, она
  отвечена, а строка снятия лежит в истории (``superseded_by_answer``);
* отмена возвращает обещание в долг, строка остаётся в истории;
* новый контекст (другая метрика, повторное завершение, другая первая
  поставка фикса) снятие не наследует: ``context_changed``, обещание снова в
  долге, освободить его может только новое решение по новому предпросмотру;
* записи без достоверного ``completed_at`` идут отдельной группой и этим шагом
  не снимаются: догадка по ``updated_at`` не основание отказаться от проверки.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub import repository
from hub.db import write_transaction
from hub.services import project_policy
from hub.services.outcome_ownership import classify_ownerships

Row = Mapping[str, Any]

PREVIEW_TTL_HOURS = 24
REASON_MAX = 500
HISTORY_MAX = 1000

# Rollup types: the release of such a task is the release of its descendants.
_ROLLUP_TYPES = frozenset({"epic", "feature"})


async def _fix_at(
    db: aiosqlite.Connection, row: Row, delivery_projects: frozenset[int]
) -> tuple[str, list[str]]:
    """The delivery part of a promise's context: first-deploy moment + delivered shas.

    The moment alone cannot see a second delivery (a later sha leaves the first
    deploy where it was), so the set of delivered shas is part of the identity.
    A repeated deploy of a known sha changes neither.
    """
    descendants = str(row.get("task_type") or "") in _ROLLUP_TYPES
    fix = await repository.first_fix_deploy_at(
        db,
        int(row["id"]),
        with_descendants=descendants,
        delivery_projects=delivery_projects,
    )
    shas = await repository.delivered_fix_shas(
        db,
        int(row["id"]),
        with_descendants=descendants,
        delivery_projects=delivery_projects,
    )
    return fix.at or "", shas


def context_of(row: Row, delivery: tuple[str, list[str]]) -> tuple[dict[str, Any], str]:
    """The promise's context and its hash: what a decision is bound to."""
    context = {
        "outcome_metric": str(row.get("outcome_metric") or "").strip(),
        "completed_at": str(row.get("completed_at") or ""),
        "fix_released_at": delivery[0],
        "delivered_shas": delivery[1],
    }
    digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return context, digest


async def retirement_states(
    db: aiosqlite.Connection,
    rows: Sequence[Row],
    answers: Mapping[int, Sequence[Any]],
) -> dict[int, dict[str, Any]]:
    """State of every task in ``rows`` that has an active (not cancelled) decision.

    ``retired`` counts only when nothing has answered the promise and its
    context is the one the decision was made for; otherwise the decision is
    history: ``superseded_by_answer`` or ``context_changed``.
    """
    active = {
        int(r["task_id"]): dict(r) for r in await repository.list_active_retirements(db)
    }
    if not active:
        return {}
    delivery = await project_policy.merge_is_delivery_projects(db)
    out: dict[int, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        decision = active.get(int(row["id"]))
        if decision is None:
            continue
        if answers.get(int(row["id"])):
            state = "superseded_by_answer"
        else:
            _, current = context_of(row, await _fix_at(db, row, delivery))
            state = (
                "retired"
                if current == decision["context_hash"]
                else ("context_changed")
            )
        out[int(row["id"])] = {
            "state": state,
            "retirement_id": decision["id"],
            "batch_id": decision["batch_id"],
            "reason": decision["reason"],
            "retired_by": decision["retired_by"],
            "retired_at": decision["retired_at"],
        }
    return out


def _normalize_cutoff(cutoff: str) -> datetime:
    """A cutoff must name an instant: a time zone is required, never guessed."""
    text = str(cutoff or "").strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "cutoff must be an ISO 8601 timestamp with a time zone, "
            "e.g. 2026-10-01T00:00:00Z"
        ) from exc
    if moment.tzinfo is None:
        raise ValueError(
            "cutoff has no time zone: write 2026-10-01T00:00:00Z or an explicit offset"
        )
    return moment.astimezone(UTC)


async def _scope_ids(db: aiosqlite.Connection, projects: Sequence[str]) -> set[int]:
    from hub.services.metrics_scope import project_task_ids

    slugs = [str(p).strip() for p in projects if str(p).strip()]
    if not slugs:
        raise ValueError(
            "projects must name at least one project: the scope is explicit"
        )
    ids: set[int] = set()
    for slug in slugs:
        raw, _ = await project_task_ids(db, slug)
        ids.update(int(i) for i in json.loads(raw))
    return ids


async def preview_retirement(
    db: aiosqlite.Connection,
    *,
    cutoff: str,
    projects: Sequence[str],
    created_by: str = "",
) -> dict[str, Any]:
    """Show the cohort a decision would cover and save it under a token.

    The cohort: completed, unanswered, not held by a valid owner, not already
    retired, in the named projects, with a trustworthy ``completed_at`` strictly
    before ``cutoff``. The rest of the unanswered, undated ones are listed
    apart. Nothing is written except the saved preview.
    """
    from hub.services import outcomes

    moment = _normalize_cutoff(cutoff)
    scope = await _scope_ids(db, projects)
    rows = [dict(r) for r in await repository.list_outcome_debt(db)]
    answers = await outcomes._answers_by_task(db)
    owners = await classify_ownerships(db, rows)
    states = await retirement_states(db, rows, answers)
    delivery = await project_policy.merge_is_delivery_projects(db)
    cohort: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    already = 0
    for row in rows:
        task_id = int(row["id"])
        if task_id not in scope or answers.get(task_id):
            continue
        if owners[task_id]["state"] == "covered":
            continue
        if states.get(task_id, {}).get("state") == "retired":
            already += 1
            continue
        finished = outcomes._parse_stamp(row["completed_at"])
        item = {
            "task_id": task_id,
            "title": row["title"],
            "completed_at": row["completed_at"],
        }
        if finished is None:
            undated.append(item)
        elif finished < moment:
            _, fingerprint = context_of(row, await _fix_at(db, row, delivery))
            cohort.append({**item, "fingerprint": fingerprint})
    token = uuid.uuid4().hex
    async with write_transaction(db):
        await repository.save_retirement_preview(
            db,
            token=token,
            cutoff=moment.isoformat(),
            projects=json.dumps([str(p) for p in projects]),
            entries=json.dumps(
                [
                    {"task_id": c["task_id"], "fingerprint": c["fingerprint"]}
                    for c in cohort
                ]
            ),
            created_by=created_by,
            ttl_hours=PREVIEW_TTL_HOURS,
        )
    return {
        "token": token,
        "cutoff": moment.isoformat(),
        "projects": [str(p) for p in projects],
        "cohort": cohort,
        "cohort_total": len(cohort),
        "undated": undated,
        "undated_total": len(undated),
        "already_retired_total": already,
        "expires_in_hours": PREVIEW_TTL_HOURS,
        "note": (
            "Nothing is retired yet. `cohort`: unanswered promises completed "
            "strictly before `cutoff`, in the named projects. `undated`: no "
            "trustworthy completed_at - listed, never retired by this step and "
            "never guessed from updated_at. Apply with the token and a reason; "
            "a promise that changed after this preview is skipped and named."
        ),
    }


async def _current_view(
    db: aiosqlite.Connection,
    task_id: int,
    delivery: frozenset[int],
) -> tuple[dict[str, Any] | None, str]:
    """The task if it is still a candidate (completed, live, has a metric)."""
    row = await repository.get_task(db, task_id)
    if row is None:
        return None, ""
    task = dict(row)
    if (
        task.get("status") != "completed"
        or task.get("archived")
        or not str(task.get("outcome_metric") or "").strip()
    ):
        return None, ""
    return task, context_of(task, await _fix_at(db, task, delivery))[1]


async def _skip_reason(
    db: aiosqlite.Connection,
    entry: Mapping[str, Any],
    delivery: frozenset[int],
    active: Mapping[int, Row],
    scope: set[int],
) -> tuple[str, dict[str, Any] | None, str]:
    """Why an entry is not applied, or "" when it is. Plus the task and its hash."""
    task_id = int(entry["task_id"])
    task, current = await _current_view(db, task_id, delivery)
    if task is None:
        return "no_longer_debt", None, ""
    if task_id not in scope:
        return "left_project_scope", task, current
    if await repository.list_outcome_answers_for_task(db, task_id):
        return "answered", task, current
    decision = active.get(task_id)
    if decision is not None and decision["context_hash"] == current:
        return "already_retired", task, current
    if current != entry["fingerprint"]:
        return "changed_since_preview", task, current
    if (await classify_ownerships(db, [task]))[task_id]["state"] == "covered":
        return "covered_by_owner", task, current
    return "", task, current


async def apply_retirement(
    db: aiosqlite.Connection,
    *,
    token: str,
    reason: str,
    retired_by: str = "",
    principal_id: int | None = None,
) -> dict[str, Any]:
    """Retire the previewed cohort; every record is re-checked in the transaction.

    All or nothing for the rows written: the decision rows and their feed
    entries share one SAVEPOINT. A record that changed since the preview is
    skipped by name, never retired quietly. A token applies ONCE: a repeat returns
    the stored outcome (``repeated``) and writes nothing, so a late retry cannot
    replay over a later cancel.
    """
    text = str(reason or "").strip()
    if not text:
        raise ValueError("reason is required: a decision without one cannot be audited")
    if len(text) > REASON_MAX:
        raise ValueError(f"reason is longer than {REASON_MAX} characters")
    async with write_transaction(db):
        stored = await repository.get_retirement_preview(db, token)
        preview = dict(stored) if stored is not None else None
        if preview is None:
            raise LookupError(
                "preview not found or expired: run the preview again and apply its token"
            )
        if preview["applied_at"] is not None:
            return {**json.loads(preview["applied_result"] or "{}"), "repeated": True}
        entries = json.loads(preview["entries"])
        scope = await _scope_ids(db, json.loads(preview["projects"]))
        delivery = await project_policy.merge_is_delivery_projects(db)
        active = {
            int(r["task_id"]): dict(r)
            for r in await repository.list_active_retirements(db)
        }
        retired: list[int] = []
        skipped: list[dict[str, Any]] = []
        await db.execute("SAVEPOINT outcome_retire_apply")
        try:
            for entry in entries:
                task_id = int(entry["task_id"])
                why, task, current = await _skip_reason(
                    db, entry, delivery, active, scope
                )
                if why:
                    skipped.append({"task_id": task_id, "reason": why})
                    continue
                await _retire_one(
                    db,
                    task or {},
                    active.get(task_id),
                    preview,
                    text,
                    retired_by,
                    principal_id,
                    current,
                    delivery,
                )
                retired.append(task_id)
        except BaseException:
            await db.execute("ROLLBACK TO outcome_retire_apply")
            await db.execute("RELEASE outcome_retire_apply")
            raise
        await db.execute("RELEASE outcome_retire_apply")
        result = {
            "batch_id": token,
            "retired": retired,
            "retired_total": len(retired),
            "skipped": skipped,
            "skipped_total": len(skipped),
            "repeated": False,
        }
        await repository.mark_retirement_preview_applied(db, token, json.dumps(result))
    return result


async def _retire_one(
    db: aiosqlite.Connection,
    task: Mapping[str, Any],
    stale: Row | None,
    preview: Row,
    reason: str,
    retired_by: str,
    principal_id: int | None,
    context_hash: str,
    delivery: frozenset[int],
) -> None:
    task_id = int(task["id"])
    if stale is not None:
        # An older decision whose context no longer holds: closed, kept in history.
        await repository.cancel_outcome_retirement(
            db,
            int(stale["id"]),
            cancelled_by=f"batch:{preview['token']}",
            reason="context changed: replaced by a new decision",
        )
    context, _ = context_of(task, await _fix_at(db, dict(task), delivery))
    await repository.insert_outcome_retirement(
        db,
        task_id=task_id,
        batch_id=preview["token"],
        reason=reason,
        retired_by=retired_by,
        principal_id=principal_id,
        cutoff=preview["cutoff"],
        context_snapshot=json.dumps(context, sort_keys=True, ensure_ascii=False),
        context_hash=context_hash,
    )


async def cancel_retirement(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    reason: str,
    cancelled_by: str = "",
    principal_id: int | None = None,
) -> dict[str, Any]:
    """Take a decision back: the promise returns to the debt, the row stays."""
    text = str(reason or "").strip()
    if not text:
        raise ValueError("reason is required")
    if len(text) > REASON_MAX:
        raise ValueError(f"reason is longer than {REASON_MAX} characters")
    async with write_transaction(db):
        active = [
            r
            for r in await repository.list_active_retirements(db)
            if int(r["task_id"]) == task_id
        ]
        if not active:
            raise ValueError(f"task #{task_id} has no active retirement to cancel")
        decision = active[0]
        await db.execute("SAVEPOINT outcome_retire_cancel")
        try:
            await repository.cancel_outcome_retirement(
                db, int(decision["id"]), cancelled_by=cancelled_by, reason=text
            )
        except BaseException:
            await db.execute("ROLLBACK TO outcome_retire_cancel")
            await db.execute("RELEASE outcome_retire_cancel")
            raise
        await db.execute("RELEASE outcome_retire_cancel")
    return {
        "task_id": task_id,
        "cancelled_retirement_id": int(decision["id"]),
        "batch_id": decision["batch_id"],
    }


async def list_retirements(
    db: aiosqlite.Connection, task_id: int | None = None
) -> dict[str, Any]:
    """The history of decisions with each one's state; cancelled rows included."""
    from hub.services import outcomes

    rows = [dict(r) for r in await repository.list_outcome_retirements(db, task_id)]
    live = [r for r in rows if r["cancelled_at"] is None]
    tasks = []
    for r in live:
        task = await repository.get_task(db, int(r["task_id"]))
        if task is not None:
            tasks.append(dict(task))
    answers = await outcomes._answers_by_task(db)
    states = await retirement_states(db, tasks, answers)
    out = []
    for r in rows[-HISTORY_MAX:]:
        if r["cancelled_at"] is not None:
            state = "cancelled"
        else:
            state = states.get(int(r["task_id"]), {}).get("state", "no_longer_debt")
        out.append({**r, "state": state})
    return {"rows": out, "total": len(rows)}
