"""Задача-состояние: сдача доказательствами, вердикт человека, завершение (#1647).

Результат такой задачи — состояние мира (сервер настроен, ключ сменён, DNS
переключён), а не коммит. Жизненный цикл тот же — draft → open → running →
review → completed, — но три шага устроены иначе, и этот модуль их и держит,
чтобы код commit-задач остался как был:

* СДАЧА (``submit_state``) — только ``submit-review`` с полем ``evidence``:
  по записи на каждый AC. Комплект пишется ВМЕСТЕ с поколением и переходом в
  review одной транзакцией и относится к СНИМКУ AC/rollback/result_kind,
  снятому в ту же минуту. Ветки, sha, PR, CI, диффа, заказа ревью нет.
* ВЕРДИКТ (``record_state_verdict``) — APPROVED ставит только человек
  (``is_human``) и только на названное поколение; без поколения или с чужим —
  409 без записей. Автопилот, стюард и агент state не завершают.
* ЗАВЕРШЕНИЕ — вместе с вердиктом, в той же условной записи (``via=
  state_approved``): git, PR и доставка не участвуют.

Ссылки на карту мест: ``.claude/state-task-call-site-map.md``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import submission_contract_violated_detail
from hub.db import log_activity, write_transaction
from hub.hub_instance import mutation_activity_detail
from hub.mcp_envelope import enrich_error_payload
from hub.models import (
    FINAL_STATUSES,
    ReviewVerdict,
    TaskEvidenceView,
    TaskReviewVerdict,
    TaskSubmitReview,
    TaskView,
)
from hub.services import prevention_gate, state_evidence, submission_contract
from hub.services.gate_pipeline import Step, run_steps
from hub.services.result_kind import is_state, qualifying_ac_count

log = logging.getLogger("hub")

#: Начало текста, которым держится «нужен возврат в работу и новая сдача».
FROZEN_HINT = (
    "нужен возврат в работу и новая сдача: человек возвращает задачу "
    "(POST /api/tasks/<id>/return-to-work), затем правка постановки и новая "
    "сдача с новым комплектом доказательств"
)

# ---------------------------------------------------------------------------
# Ошибки: одна форма на всё, без входных значений
# ---------------------------------------------------------------------------

_KIND_TEXT = {
    state_evidence.KIND_TYPE: "значение не того типа",
    state_evidence.KIND_EMPTY_SET: "комплект доказательств пуст",
    state_evidence.KIND_TOO_MANY: "записей больше допустимого",
    state_evidence.KIND_UNKNOWN_FIELD: "в записи есть неизвестное поле",
    state_evidence.KIND_MISSING_FIELD: "в записи нет обязательного поля",
    state_evidence.KIND_EMPTY_VALUE: "значение пусто",
    state_evidence.KIND_TOO_LONG: "значение длиннее допустимого",
    state_evidence.KIND_CREDENTIAL: (
        "значение похоже на секрет — передайте обезличенное наблюдение"
    ),
    state_evidence.KIND_DUPLICATE_AC: "на один AC две записи",
    state_evidence.KIND_UNKNOWN_AC: "запись на AC, которого у задачи нет",
    state_evidence.KIND_MISSING_AC: "нет записи на AC",
}


def evidence_refusal(exc: state_evidence.EvidenceError) -> HTTPException:
    """422 для ошибки контракта доказательств. Входных значений в нём нет."""
    where = f" (поле {exc.field})" if exc.field else ""
    named = f" AC: {', '.join(exc.ac_ids)}." if exc.ac_ids else ""
    return HTTPException(
        422,
        detail=enrich_error_payload(
            {
                "reason": "state_evidence_invalid",
                "kind": exc.kind,
                "ac_ids": exc.ac_ids,
                "field": exc.field,
                "pattern": exc.pattern,
                "actor_hint": "agent",
                "retry_by_same_caller": True,
                "message": f"{_KIND_TEXT.get(exc.kind, exc.kind)}{where}.{named}",
                "hint": (
                    "Ничего не записано: ни поколения, ни доказательств, статус "
                    "не менялся. Нужна ровно одна запись {ac_id, action, "
                    "observed, target, observed_at} на каждый AC задачи; "
                    "наблюдения обезличены — значения из запроса в ответе не "
                    "повторяются."
                ),
                "suggested_tool": "hub_submit_for_review",
            }
        ),
    )


def _frozen_refusal(task: dict[str, Any], what: str) -> HTTPException:
    return HTTPException(
        409,
        detail=enrich_error_payload(
            {
                "reason": "state_statement_frozen",
                "actor_hint": "human",
                "current_status": task.get("status", ""),
                "message": (
                    f"{what} задачи-состояния после сдачи не меняется: {FROZEN_HINT}."
                ),
                "hint": FROZEN_HINT,
            }
        ),
    )


# ---------------------------------------------------------------------------
# Заморозка постановки
# ---------------------------------------------------------------------------


def statement_is_frozen(task: dict[str, Any]) -> bool:
    """Постановка state-задачи заморожена: сдача была, явного возврата на ней нет.

    Признак — ``tasks.unfrozen_generation``: поколение, на котором человек
    вернул задачу в работу (return-to-work, rework). Статус не значим: вопрос
    агента (needs_info) или уход в open по реестру сессий заморозку не снимают.
    Новая сдача поднимает поколение, и постановка замерзает снова.
    """
    generation = int(task.get("submission_generation") or 0)
    return (
        is_state(task)
        and generation > 0
        and int(task.get("unfrozen_generation") or 0) != generation
    )


def refuse_if_frozen(task: dict[str, Any] | None, what: str) -> None:
    """Отказ правке AC/rollback/result_kind замороженной state-задачи (#1647)."""
    if task is not None and statement_is_frozen(task):
        raise _frozen_refusal(task, what)


# ---------------------------------------------------------------------------
# Профиль готовности, перепроверяемый на сдаче
# ---------------------------------------------------------------------------


def _statement_gaps(task: dict[str, Any], ac_rows: list[dict[str, Any]]) -> list[str]:
    """Чего не хватает постановке state-задачи (DoR могли обойти force)."""
    gaps: list[str] = []
    if not (task.get("rollback") or "").strip():
        gaps.append("rollback пуст")
    if qualifying_ac_count(ac_rows) == 0:
        gaps.append("нет AC с verifiable_by из manual, log_check, ui_check")
    return gaps


def _require_submittable(task: dict[str, Any]) -> None:
    if task.get("job_id"):
        raise HTTPException(
            400,
            "state tasks are never dispatched: a job_id here is a defect, "
            "submit-review is the only way to hand a state task over",
        )
    if task["status"] not in ("running", "review"):
        raise HTTPException(
            400,
            "can only submit running or under-review state tasks for review, "
            f"current status: {task['status']}",
        )


def _snapshot(task: dict[str, Any], ac_rows: list[dict[str, Any]]) -> str:
    """Снимок постановки на момент сдачи: result_kind, rollback, AC."""
    payload = {
        "result_kind": "state",
        "rollback": (task.get("rollback") or "").strip(),
        "acceptance_criteria": [
            {
                "id": str(row["ac_id"]),
                "given": row["given"],
                "when": row["when_clause"],
                "then": row["then_clause"],
                "verifiable_by": row["verifiable_by"],
                "test_ref": row["test_ref"],
            }
            for row in ac_rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


async def _check_contract(
    db: aiosqlite.Connection, task_id: int, body: TaskSubmitReview
) -> str:
    """Контракт сдачи для state: summary обязателен, model — декларация.

    Мутации неприменимы: проверки AC нет, а хаб команд не исполняет. Возвращает
    запись для ленты при warn, пустую строку иначе.
    """
    from hub.services.project_policy import (
        CONTRACT_OFF,
        CONTRACT_REQUIRE,
        gate_policy_for_task,
        submission_contract_of,
    )

    if not (body.summary or "").strip():
        raise HTTPException(
            422,
            detail=enrich_error_payload(
                {
                    "reason": "state_summary_required",
                    "actor_hint": "agent",
                    "retry_by_same_caller": True,
                    "message": "summary пуст: опишите, что выполнено и что увидели",
                    "hint": "Ничего не записано. Повторите сдачу с непустым summary.",
                    "suggested_tool": "hub_submit_for_review",
                }
            ),
        )
    mode = submission_contract_of(await gate_policy_for_task(db, task_id))
    if mode == CONTRACT_OFF or (body.model or "").strip():
        return ""
    found = ["model пуст — назовите модель, написавшую сдачу (#758)"]
    if mode == CONTRACT_REQUIRE:
        raise HTTPException(
            422,
            detail=submission_contract_violated_detail(
                found,
                mutations_format="мутации для задачи-состояния не применяются",
            ),
        )
    return submission_contract.warning_text(found)


# ---------------------------------------------------------------------------
# Сдача
# ---------------------------------------------------------------------------


async def evidence_views(
    db: aiosqlite.Connection, task_id: int
) -> list[TaskEvidenceView]:
    """Доказательства всех поколений для карточки; пусто, если их не было."""
    return [
        TaskEvidenceView(**dict(row))
        for row in await repo.list_task_evidence(db, task_id)
    ]


async def submit_state(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
    body: TaskSubmitReview,
    *,
    principal_id: int | None,
) -> TaskView:
    """Сдача state-задачи: доказательства по снимку AC, без git, CI и ревью.

    Пропущены нарочно (карта мест): пин sha, ci_before_submit, bug_red_test,
    дифф и пересчёт риска, поверхности, submit_rules, path_notices, поиск и
    открытие PR; в переходе — принятие отчёта CI, предупреждение о стеке
    веток, заказ ревью и подсказка про machine-review. Всё чтение, проверка и
    запись идут под одной write-транзакцией: снимок AC и комплект
    доказательств не могут разойтись с тем, что видит проверка.
    """
    from hub.services import lifecycle as lc

    _require_submittable(task)
    async with write_transaction(db):
        fresh = dict(lc._existing_task(await repo.get_task(db, task_id), task_id))
        _require_submittable(fresh)
        ac_rows = [dict(r) for r in await repo.list_acceptance_criteria(db, task_id)]
        gaps = _statement_gaps(fresh, ac_rows)
        if gaps:
            raise HTTPException(
                422,
                detail=enrich_error_payload(
                    {
                        "reason": "state_statement_incomplete",
                        "actor_hint": "human",
                        "message": "постановка state-задачи неполна: "
                        + "; ".join(gaps),
                        "hint": FROZEN_HINT,
                    }
                ),
            )
        prevention = await prevention_gate.check_submission(db, fresh, body.prevention)
        try:
            items = state_evidence.validate_evidence(
                body.evidence, [str(r["ac_id"]) for r in ac_rows]
            )
        except state_evidence.EvidenceError as exc:
            raise evidence_refusal(exc) from None
        contract_alert = await _check_contract(db, task_id, body)

        was_review = fresh["status"] == "review"
        if not await repo.transition_status_if(
            db, task_id, expected_from=fresh["status"], new_status="review"
        ):
            raise HTTPException(
                409,
                f"Task #{task_id} left {fresh['status']} state during submit; "
                "retry from its current status",
            )
        generation = await repo.bump_submission_generation(db, task_id)
        declared_model = (body.model or "").strip()[:100]
        await repo.update_task(
            db,
            task_id,
            submission_sha="",
            submission_model=declared_model,
            review_job_id=None,
        )
        await repo.record_submission(
            db,
            task_id=task_id,
            generation=generation,
            sha="",
            base_branch="",
            state_snapshot=_snapshot(fresh, ac_rows),
        )
        signature = (body.agent or fresh.get("assigned_agent") or "").strip()[:100]
        await repo.insert_task_evidence(
            db,
            task_id=task_id,
            generation=generation,
            items=items,
            principal_id=principal_id,
            agent=signature,
        )
        if prevention is not None:
            await prevention_gate.store_prevention(
                db, task_id, prevention, actor=signature or "agent"
            )
        text = (
            f"{repo.SUBMISSION_UPDATE_PREFIX}{generation}). Задача-состояние: "
            f"доказательства по {len(items)} AC приняты без ветки, PR и CI."
        )
        if was_review:
            text += " Пересдача из review: вердикт по прежней сдаче больше не текущий."
        if declared_model:
            text += f" Модель исполнителя (декларация): {declared_model}."
        text += f" {body.summary.strip()}"
        await repo.add_task_update(
            db,
            task_id,
            signature or fresh.get("assigned_agent") or "",
            "status",
            text,
            principal_id=principal_id,
            author_kind="principal" if principal_id is not None else "anonymous",
        )
        if contract_alert:
            await repo.add_task_update(db, task_id, "hub", "alert", contract_alert)
        await repo.insert_event(
            db,
            kind="state_submitted",
            task_id=task_id,
            actor=signature or "agent",
            payload={"generation": generation, "evidence": len(items)},
        )
        await db.commit()
    await log_activity(
        db,
        "task_submitted_for_review",
        f"Task #{task_id} submitted for review (generation {generation}, state)",
        detail=mutation_activity_detail(),
    )
    row = lc._existing_task(await repo.get_task(db, task_id), task_id)
    view = lc.row_to_task(row, updates=await repo.get_task_updates(db, task_id))
    view.evidence = await evidence_views(db, task_id)
    view.wait_baseline = lc.wait_baseline_for(dict(row))
    view.lifecycle_hint = (
        f"Доказательства поколения {generation} приняты. Вердикт выносит человек "
        f"и обязан назвать expected_generation={generation}; "
        "агентский APPROVED по задаче-состоянию отказывается."
    )
    return view


# ---------------------------------------------------------------------------
# Вердикт
# ---------------------------------------------------------------------------


def _human_only_refusal(task_id: int) -> HTTPException:
    return HTTPException(
        403,
        detail=enrich_error_payload(
            {
                "reason": "state_approval_requires_human",
                "actor_hint": "human",
                "message": (
                    f"APPROVED по задаче-состоянию #{task_id} ставит только "
                    "человек: автопилот, стюард и агенты её не завершают."
                ),
                "hint": (
                    "Агентский changes_requested допустим. APPROVED — человек, "
                    "с expected_generation (кнопка на карточке или "
                    "hp-hub review-verdict --expected-generation N)."
                ),
            }
        ),
    )


def _generation_refusal(
    task_id: int, expected: int | None, current: int
) -> HTTPException:
    named = "не назван" if expected is None else str(expected)
    return HTTPException(
        409,
        detail=enrich_error_payload(
            {
                "reason": "state_verdict_generation_mismatch",
                "actor_hint": "human",
                "message": (
                    f"вердикт по задаче-состоянию #{task_id} привязан к "
                    f"поколению сдачи: expected_generation {named}, живое — "
                    f"{current}. Ничего не записано."
                ),
                "hint": (
                    "Прочитайте доказательства живого поколения на карточке и "
                    f"повторите вердикт с expected_generation={current}."
                ),
                "current_generation": current,
            }
        ),
    )


def _gate_the_verdict(
    task: dict[str, Any], body: TaskReviewVerdict, human_caller: bool
) -> int:
    """Кто и о каком поколении судит; возвращает поколение, на которое пишем."""
    current = int(task.get("submission_generation") or 0)
    if body.verdict == ReviewVerdict.approved and not human_caller:
        raise _human_only_refusal(int(task["id"]))
    if body.expected_generation is None or body.expected_generation != current:
        raise _generation_refusal(int(task["id"]), body.expected_generation, current)
    return current


async def _completion_hold(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[str, str]:
    """Что удерживает завершение: (код, текст) или ("", "").

    Те же два основания, что у done-отчёта commit-задачи: прод-дефект без вывода
    (#919) и блокер, записанный после последней сдачи (#948). Они не
    отменяются тем, что задача — состояние.
    """
    from hub.services import lifecycle as lc

    gap = prevention_gate.prevention_gap(task)
    if gap:
        return "prevention_missing", f"Прод-дефект не завершён (state_approved): {gap}"
    updates = [dict(u) for u in await repo.get_task_updates(db, int(task["id"]))]
    holding = lc.blocker_holding_the_done_report(updates)
    if holding is not None:
        return (
            "live_blocker",
            "Задача-состояние одобрена, но после последней сдачи записан "
            f"блокер — {lc.blocker_note(holding)}.",
        )
    return "", ""


async def _verdict_steps(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
    body: TaskReviewVerdict,
    principal_id: int | None,
):
    """Содержательные проверки вердикта — те же функции, что у commit-пути."""
    from hub.services import lifecycle as lc

    state = lc.VerdictContext(
        db=db, task_id=task_id, task=task, body=body, principal_id=principal_id
    )
    await run_steps(
        state,
        (
            Step("has_a_submission", lc._vstep_has_a_submission),
            Step(
                "changes_requested_has_content",
                lc._vstep_changes_requested_has_content,
            ),
            Step("verdict_matches_its_text", lc._vstep_verdict_matches_its_text),
            Step("verdict_is_not_a_repeat", lc._vstep_verdict_is_not_a_repeat),
            Step(
                "changes_requested_has_in_scope_finding",
                lc._vstep_changes_requested_has_in_scope_finding,
            ),
        ),
    )
    return state


async def record_state_verdict(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
    body: TaskReviewVerdict,
    *,
    principal_id: int | None,
    human_caller: bool,
) -> TaskView:
    """Вердикт по state-задаче: человек, поколение, одна условная запись.

    APPROVED завершает задачу той же записью (``via=state_approved``);
    CHANGES_REQUESTED возвращает её в running без git. Запись условна по
    статусу, поколению и result_kind в самом SQL: расхождение — 409, и
    транзакция откатывается целиком, ни вердикта, ни строки ленты, ни события.
    """
    from hub.services import lifecycle as lc
    from hub.services.sessions import note_session_task

    if int(task.get("submission_generation") or 0) == 0:
        raise HTTPException(
            400,
            "no submission to review yet: the task has never been submitted for review",
        )
    generation = _gate_the_verdict(task, body, human_caller)
    state = await _verdict_steps(db, task_id, task, body, principal_id)

    approved = body.verdict == ReviewVerdict.approved
    async with write_transaction(db):
        fresh = dict(lc._existing_task(await repo.get_task(db, task_id), task_id))
        hold_code, hold_text = ("", "")
        if approved:
            hold_code, hold_text = await _completion_hold(db, fresh)
        target = (
            ("needs_decision" if hold_code else "completed") if approved else "running"
        )
        findings_json = json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings],
            ensure_ascii=False,
        )
        if not await repo.record_state_decision(
            db,
            task_id,
            verdict=body.verdict.value,
            generation=generation,
            findings_json=findings_json,
            new_status=target,
        ):
            raise HTTPException(
                409,
                detail=enrich_error_payload(
                    {
                        "reason": "state_verdict_conflict",
                        "actor_hint": "human",
                        "message": (
                            f"вердикт о сдаче {generation} не записан: поколение "
                            "уже другое, задача не в review или вердикт на это "
                            "поколение уже стоит. Ничего не записано."
                        ),
                        "hint": "Перечитайте карточку и повторите с живым поколением.",
                    }
                ),
            )
        agent, content = lc._verdict_update_text(state)
        content += (
            f"\nПо доказательствам поколения {generation}, задача-состояние "
            "(без ветки, PR и CI)."
        )
        await repo.add_task_update(
            db,
            task_id,
            agent,
            "review",
            content,
            principal_id=principal_id,
            author_kind="principal" if principal_id is not None else "anonymous",
        )
        await repo.insert_event(
            db,
            kind="review_verdict_recorded",
            task_id=task_id,
            actor=agent,
            payload=lc._verdict_event_payload(state, False),
        )
        if body.verdict == ReviewVerdict.changes_requested:
            await repo.update_task(
                db, task_id, review_cycle=int(fresh.get("review_cycle") or 0) + 1
            )
        if hold_code:
            await repo.add_task_update(db, task_id, "hub", "alert", hold_text)
            await repo.insert_event(
                db,
                kind="needs_decision",
                task_id=task_id,
                actor=agent,
                payload={"reason": hold_code, "via": "state_approved"},
            )
        elif approved:
            await repo.insert_event(
                db,
                kind="task_completed",
                task_id=task_id,
                actor=agent,
                payload={"via": "state_approved", "generation": generation},
            )
            await note_session_task(db, fresh.get("claim_session_id") or "", None)
            await lc.maybe_rollup_parent(db, task_id, commit=False)
        await db.commit()
    await log_activity(
        db,
        "task_review_verdict",
        f"Task #{task_id} review verdict: {body.verdict.value} (state)",
        detail=mutation_activity_detail(),
    )
    row = await repo.get_task(db, task_id)
    view = lc.row_to_task(row, updates=await repo.get_task_updates(db, task_id))  # type: ignore[arg-type]
    view.evidence = await evidence_views(db, task_id)
    if hold_code:
        view.lifecycle_hint = hold_text
    return view


# ---------------------------------------------------------------------------
# Done-отчёт
# ---------------------------------------------------------------------------


async def rework_without_dispatch(db: aiosqlite.Connection, task_id: int) -> None:
    """Rework state-задачи после арбитража: в open, без задания (#1647).

    Тот же исход, что у fix-пути без job_id, но осознанный: держатель и сессия
    сняты, чтобы задачу мог взять любой. Коммит — здесь, как у commit-пути.
    """
    from hub.services.sessions import note_session_task

    row = await repo.get_task(db, task_id)
    session = str(dict(row).get("claim_session_id") or "") if row is not None else ""
    await repo.update_task(
        db,
        task_id,
        status="open",
        unfrozen_generation=int(dict(row).get("submission_generation") or 0)
        if row is not None
        else 0,
        job_id=None,
        review_job_id=None,
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
        implementer_principal_id=None,
    )
    await note_session_task(db, session, None)
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        "Rework по решению человека: задача-состояние возвращена в open без "
        "headless-задания; постановку можно править, затем новая сдача.",
    )
    await db.commit()


def refuse_done_report(task: dict[str, Any]) -> None:
    """done-отчёт state-задачи не сдача и не завершение (#1647).

    Зовётся до вставки строки отчёта: отказ не оставляет ни done-строки, ни
    bump поколения, ни перехода — при любом ``auto_review``. Завершённую задачу
    это не трогает (остаётся прежняя обработка терминального статуса).
    /decide accept и /force-complete — аудируемые исключения и сюда не ходят.
    """
    if not is_state(task) or task.get("status") in FINAL_STATUSES:
        return
    raise HTTPException(
        409,
        detail=enrich_error_payload(
            {
                "reason": "state_done_report_refused",
                "actor_hint": "agent",
                "current_status": task.get("status", ""),
                "message": (
                    "done-отчёт по задаче-состоянию не завершает её и не "
                    "заменяет сдачу: доказательства принимает только "
                    "submit-review с полем evidence, завершает — человек."
                ),
                "hint": (
                    "hub_submit_for_review(task_id, summary, evidence=[{ac_id, "
                    "action, observed, target, observed_at}, ...]) — по записи "
                    "на каждый AC."
                ),
                "suggested_tool": "hub_submit_for_review",
            }
        ),
    )


def refusal_for_door(door: str) -> HTTPException:
    """422 для двери исполнителя: headless и облачный исполнитель не для state."""
    from hub.services.result_kind import AUTOMATION_REFUSAL

    return HTTPException(
        422,
        detail=enrich_error_payload(
            {
                "reason": "state_task_no_executor",
                "actor_hint": "human",
                "door": door,
                "message": f"{AUTOMATION_REFUSAL}.",
                "hint": (
                    "Задачу-состояние берёт человек или агент в ручном pair: "
                    "claim, pair-start, работа в мире, submit-review с evidence."
                ),
            }
        ),
    )


__all__ = [
    "evidence_refusal",
    "evidence_views",
    "record_state_verdict",
    "refuse_done_report",
    "refuse_if_frozen",
    "refusal_for_door",
    "statement_is_frozen",
    "submit_state",
]
