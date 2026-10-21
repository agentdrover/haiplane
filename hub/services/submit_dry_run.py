"""Сдача без записи: ``POST /api/tasks/{id}/submit-review?dry_run=1`` (#1975).

Launcher k8 узнавал, что payload сдачи не сходится с правилами хаба, только на
настоящей сдаче: 422 приходил после заморозки ВМ, и исправить данные было уже
некому. Пробная сдача проходит ТЕ ЖЕ шаги в том же порядке и отвечает тем же
кодом и тем же detail, но ничего не пишет.

Конвейер не копируется: шаги берутся из ``SUBMIT_STEPS_BEFORE_SAME_SHA_CHECK`` и
``SUBMIT_STEPS_AFTER_SAME_SHA_CHECK``, из второго списка исключён один
``delivery_pr`` (push и PR). Новый шаг сдачи попадает сюда сам. Что в шагах
пишет, разделено с вычислением: CI-проекцию считает ``ci_trust.preview_report``,
отказ по expected_sha — ``expected_sha_gate.head_refusal``; здесь не открыта ни
одна транзакция (обёртка существующего конвейера в BEGIN/ROLLBACK запрещена:
сеть под ``BEGIN IMMEDIATE`` держит общий lock).

Допустимые побочные эффекты названы в docs/agent-context/contracts.md:
``first_used_at`` токена сессии, ``git fetch``, кэш refs, ``_observed_tips``.
Успех пробной сдачи не гарантирует успеха настоящей: та перепроверяет состояние
и CI под блокировкой.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hub import config
from hub import repository as repo
from hub.mcp_envelope import enrich_error_payload
from hub.models import TaskSubmitReview
from hub.outcome_hint import OutcomeHintView
from hub.services import finding_outcome, gate_pipeline, lifecycle, path_notices
from hub.services import project_policy as policy
from hub.services.result_kind import is_state

#: Шаги этого пути, чья причина «не здесь» — запись или публикация.
SKIPPED_STEPS = frozenset({"delivery_pr"})

#: Шаги, которые сами читают режим из политики проекта и в режиме off ничего не
#: проверяют. Конвейер видит их как «always», поэтому режим спрашивается здесь.
_PROJECT_MODES = {
    "submission_contract": policy.submission_contract_of,
    "bug_red_test": policy.bug_red_test_of,
    "ci_before_submit": policy.ci_before_submit_of,
}


class SubmitDryRunView(BaseModel):
    """Ответ пробной сдачи: что случилось бы, без записей (#1975)."""

    dry_run: bool = True
    would_submit: bool = True
    same_sha_noop: bool = False
    #: Шаги конвейера в порядке исполнения; шаг в режиме off не входит.
    passed_steps: list[str] = Field(default_factory=list)
    outcome_hint: OutcomeHintView | None = None
    #: Предупреждения warn-гейтов и path_notices (только compute).
    notices: list[str] = Field(default_factory=list)


def state_task_refusal(task_id: int) -> HTTPException:
    """422: у задачи-состояния проверка доказательств без записи не строится."""
    return HTTPException(
        422,
        detail=enrich_error_payload(
            {
                "reason": "dry_run_unsupported_for_state",
                "actor_hint": "agent",
                "retry_by_same_caller": False,
                "task_id": task_id,
                "message": (
                    f"Task #{task_id}: задача-состояние (result_kind=state) "
                    "сдаётся доказательствами, и пробной сдачи для неё нет."
                ),
                "hint": (
                    "Nothing was recorded. Сдайте задачу-состояние настоящей "
                    "сдачей: формат evidence проверит она."
                ),
            }
        ),
    )


async def _ran(state: lifecycle.SubmitContext, step: gate_pipeline.Step) -> bool:
    """Шаг выполнялся (не неактивен и не в режиме off — конвейера или проекта)."""
    if not step.active or step.mode() == gate_pipeline.OFF:
        return False
    read = _PROJECT_MODES.get(step.name)
    if read is None:
        return True
    mode = read(await policy.gate_policy_for_task(state.db, state.task_id))
    return mode != gate_pipeline.OFF


async def _run(
    state: lifecycle.SubmitContext,
    steps: tuple[gate_pipeline.Step, ...],
    passed: list[str],
) -> None:
    """Те же шаги тем же ``run_steps``, по одному — чтобы назвать пройденные."""
    for step in steps:
        if step.name in SKIPPED_STEPS:
            continue
        await gate_pipeline.run_steps(state, (step,))
        if await _ran(state, step):
            passed.append(step.name)


async def _same_sha(state: lifecycle.SubmitContext, passed: list[str]) -> None:
    """Пересдача того же sha из review: логика ``_same_sha_noop_response`` без записей.

    Дифф и область при accept_areas считаются и не сохраняются; исходы находок
    планируются тем же ``plan_outcomes`` с тем же фильтром отвеченных uid;
    ``record`` заметок путей, заказ ревью и запись выбросов сюда не входят.
    """
    db, task_id, body = state.db, state.task_id, state.body
    generation = int(state.task.get("submission_generation") or 0)
    if body.accept_areas:
        await lifecycle._step_resolve_diff(state)
        passed.append("resolve_diff")
        state.gate_mode = ""  # вне конвейера: читай политику (как у настоящего повтора)
        await lifecycle._step_surfaces(state)
        if gate_pipeline.policy("SDD_SURFACES")() != gate_pipeline.OFF:
            passed.append("surfaces")
    open_items = await finding_outcome.open_findings(db, task_id, generation)
    pending = await finding_outcome.items_still_to_plan(
        db, task_id, generation, open_items, body.finding_outcomes
    )
    try:
        _, still_open = finding_outcome.plan_outcomes(open_items, pending)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if gate_pipeline.policy("FINDING_OUTCOME")() != gate_pipeline.OFF:
        passed.append("finding_outcomes")
    if (config.FINDING_OUTCOME or "warn").strip().lower() != "off":
        state.outcome_hint = finding_outcome.build_hint(still_open)


async def _notices(state: lifecycle.SubmitContext, generation: int) -> list[str]:
    lines = [
        state.surface_note,
        state.outcome_note,
        *state.rule_lines,
        *state.unchecked_lines,
        state.contract_alert,
        state.ci_before_alert,
        state.risk_alert,
        state.red_test_record if state.red_test_kind == "alert" else "",
    ]
    found = state.path_notices
    if found is not None and found.state != path_notices.STATE_NONE:
        lines.append(
            path_notices.render_text(
                generation + 1, found.sha, found.state, found.reason, found.notices
            )
        )
    if state.same_sha_noop:
        saved = await path_notices.view_for_generation(
            state.db, state.task_id, generation
        )
        lines.append(path_notices.shown_text(saved))
    return [line for line in lines if line]


async def dry_run_submit(
    db, task_id: int, body: TaskSubmitReview | None = None
) -> SubmitDryRunView:
    """Пробная сдача pair-задачи: тот же отказ, что у настоящей, и ни одной записи."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if is_state(task):
        raise state_task_refusal(task_id)
    state = lifecycle.SubmitContext(
        db=db, task_id=task_id, task=task, body=body or TaskSubmitReview(), dry_run=True
    )
    passed: list[str] = []
    await _run(state, lifecycle.SUBMIT_STEPS_BEFORE_SAME_SHA_CHECK, passed)
    if state.same_sha_noop:
        await _same_sha(state, passed)
    else:
        await _run(state, lifecycle.SUBMIT_STEPS_AFTER_SAME_SHA_CHECK, passed)
    generation = int(task.get("submission_generation") or 0)
    return SubmitDryRunView(
        same_sha_noop=state.same_sha_noop,
        passed_steps=passed,
        outcome_hint=state.outcome_hint,
        notices=await _notices(state, generation),
    )
