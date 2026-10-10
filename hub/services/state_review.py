"""Задача-состояние на гейте: один читатель для очереди, входящих, брифа и карточки (#1648).

Ядро (#1647) умеет принять сдачу доказательствами и завершить задачу вердиктом
человека. Эта часть отвечает на вопрос «что человек видит и куда его ведут»:
без неё state-задача в review выглядела как «ждём машинный отчёт», а бриф и
карточка показывали отсутствующий CI и «тесты 0 из 0».

ПРАВИЛО ОДНО. Читатели (``review_queue``, ``inbox_decisions``, ``verdict_route``,
``review_brief``, веб-карточка) не сравнивают вид задачи со строкой и не
считают полноту доказательств сами: они зовут ``current_submission`` и
``state_review_view`` отсюда. Вид задачи узнаёт ``result_kind.is_state`` /
``automation_not_applicable`` — тот же предикат, что у дверей автоматики.

БЮДЖЕТ. Всё, что здесь читается, — строки БД: задача, снимок сдачи, таблица
доказательств. Ни git, ни сети, ни обхода репозитория: бриф state обязан
укладываться в секунды, и на проде бриф #1647 в 15 с не укладывался именно из-за
последовательных блоков кода. Кодовые блоки для state не вычисляются вовсе —
они отдаются готовым ответом «не применимо» (``NOT_APPLICABLE_CHECKS``).

«Не применимо» — не ``unknown`` и не ``match``: первое значит «не смогли
узнать», второе — «сверили и совпало». Здесь сверять нечего.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.models import (
    BaseMergeState,
    CallSiteSection,
    CIRunReportState,
    EvidenceCoverage,
    GenerationReview,
    LiveCheckState,
    PrepassState,
    ReviewBrief,
    StateEvidenceItem,
    StateReviewView,
    TaskProjectRef,
    ValidationStanding,
)
from hub.services.result_kind import (
    AUTOMATION_REFUSAL,
    NOT_APPLICABLE,
    is_state,
)

#: Код причины для ``GenerationReview.reason`` и строк очереди.
STATE_REASON = "state_task"

#: Проверки кода, которых у задачи-состояния нет, и почему. Порядок — порядок
#: показа. Имена совпадают с именами блоков брифа, чтобы читатель находил
#: ответ там же, где ищет обычный.
NOT_APPLICABLE_CHECKS: tuple[tuple[str, str], ...] = (
    ("sha_check", "у задачи нет ветки и закреплённого коммита: сверять нечего"),
    ("ci_run_report", "кода нет, CI не запускается"),
    ("ac_tests", "AC подтверждаются наблюдениями, а не тестами"),
    ("base_merge", "мержить нечего: ветки и PR нет"),
    ("path_notices", "диффа нет, пути не проверяются"),
    ("call_sites", "диффа нет, вызовы символов не ищутся"),
    ("machine_review", AUTOMATION_REFUSAL),
)


def not_applicable_checks() -> list[dict[str, str]]:
    return [{"check": name, "reason": why} for name, why in NOT_APPLICABLE_CHECKS]


@dataclass
class StateSubmission:
    """Сдача state-задачи ТЕКУЩЕГО поколения: снимок, доказательства, полнота."""

    generation: int
    snapshot: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def snapshot_ac_ids(self) -> list[str]:
        return [
            str(ac.get("id"))
            for ac in self.snapshot.get("acceptance_criteria") or []
            if ac.get("id")
        ]

    @property
    def missing_ac(self) -> list[str]:
        covered = {str(e["ac_id"]) for e in self.evidence}
        return [ac for ac in self.snapshot_ac_ids if ac not in covered]

    @property
    def complete(self) -> bool:
        """Снимок записан и КАЖДЫЙ его AC имеет запись поколения."""
        return bool(self.snapshot_ac_ids) and not self.missing_ac


def _parse_snapshot(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def current_submission(
    db: aiosqlite.Connection, task: Mapping[str, Any]
) -> StateSubmission | None:
    """Сдача текущего поколения state-задачи; ``None`` — не state или сдач не было.

    Берёт ТОЛЬКО текущее поколение: комплекты прошлых сдач лежат в таблице
    навсегда (insert-only), и вердикт по этой сдаче не должен опираться на то,
    что автор наблюдал при прошлой. Снимок и доказательства читаются по
    ``(task_id, generation)`` — двум чтениям БД, без git.
    """
    if not is_state(task):
        return None
    generation = int(task["submission_generation"] or 0)
    if generation <= 0:
        return None
    task_id = int(task["id"])
    submission = await repo.get_submission(db, task_id, generation)
    snapshot = (
        _parse_snapshot(dict(submission).get("state_snapshot"))
        if submission is not None
        else {}
    )
    evidence = [dict(r) for r in await repo.list_task_evidence(db, task_id, generation)]
    return StateSubmission(generation=generation, snapshot=snapshot, evidence=evidence)


def _author(row: Mapping[str, Any]) -> str:
    signed = str(row.get("agent") or "").strip()
    if signed:
        return signed
    principal = row.get("principal_id")
    return f"принципал #{principal}" if principal is not None else "автор не назван"


def _headline(sub: StateSubmission) -> str:
    if sub.complete:
        return (
            f"Задача-состояние: доказательства поколения {sub.generation} по "
            f"{len(sub.evidence)} AC. Ветки, PR и CI нет, машинного ревью не "
            "будет — вердикт выносит человек."
        )
    if not sub.snapshot_ac_ids:
        return (
            f"Задача-состояние: снимок AC поколения {sub.generation} не записан — "
            "принимать не по чему."
        )
    return (
        f"Задача-состояние: у поколения {sub.generation} нет записей по "
        + ", ".join(sub.missing_ac)
        + " — комплект неполон."
    )


async def state_view_of(
    db: aiosqlite.Connection, task: Mapping[str, Any]
) -> StateReviewView:
    """Блок «что принимается» для строки, про которую вызывающий УЖЕ знает, что
    она state (он ветвится по ``automation_not_applicable``)."""
    sub = await current_submission(db, task)
    if sub is None:
        return StateReviewView(
            generation=0,
            rollback=str(task["rollback"] or "").strip(),
            not_applicable=not_applicable_checks(),
            headline="Задача-состояние: сдачи ещё не было.",
        )
    return StateReviewView(
        generation=sub.generation,
        expected_generation=sub.generation,
        rollback=str(sub.snapshot.get("rollback") or "").strip(),
        acceptance_criteria=list(sub.snapshot.get("acceptance_criteria") or []),
        evidence=[StateEvidenceItem(**e, author=_author(e)) for e in sub.evidence],
        evidence_complete=sub.complete,
        missing_ac=sub.missing_ac,
        not_applicable=not_applicable_checks(),
        headline=_headline(sub),
    )


async def state_review_view(
    db: aiosqlite.Connection, task: Mapping[str, Any]
) -> StateReviewView | None:
    """То же для произвольной строки; ``None`` — не state."""
    return await state_view_of(db, task) if is_state(task) else None


def state_generation_review(
    task: Mapping[str, Any], view: StateReviewView
) -> GenerationReview:
    """Ответ «есть ли ревью у сдачи» для state: машинного нет по определению."""
    return GenerationReview(
        generation=int(task["submission_generation"] or 0),
        has_review=False,
        reason=STATE_REASON,
        headline=view.headline,
    )


def state_coverage(view: StateReviewView) -> EvidenceCoverage:
    """Покрытие брифа для state: полнота доказательств, а не наличие CI."""
    ran = [f"evidence:{e.ac_id}" for e in view.evidence]
    missing = [
        {"check": f"evidence:{ac}", "reason": "по критерию нет записи этого поколения"}
        for ac in view.missing_ac
    ]
    if not view.acceptance_criteria:
        missing.append({"check": "snapshot", "reason": "снимок AC сдачи не записан"})
    complete = view.evidence_complete and not missing
    return EvidenceCoverage(
        state="complete" if complete else ("partial" if ran else "none"),
        headline=view.headline,
        checks_ran=ran,
        checks_missing=missing,
        checks_not_applicable=view.not_applicable,
    )


async def build_state_brief(
    db: aiosqlite.Connection, task_id: int, row: Any
) -> ReviewBrief:
    """Бриф задачи-состояния: только чтения БД, кодовые блоки — «не применимо».

    Поля формы ``ReviewBrief`` те же, что у commit-брифа, чтобы клиенты не
    ветвились по форме: у блоков кода вместо ``unknown``/``match`` стоит
    ``not_applicable`` с причиной, а то, чего у state нет вовсе (отчёт,
    круг ревью, PR), пусто.
    """
    from hub import services
    from hub.services import review_evidence
    from hub.services.verdict_route import verdict_route

    task_row = dict(row)
    task_view = services.row_to_task(row)
    project_row = await repo.resolve_project_for_task(db, task_id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    view = await state_view_of(db, task_row)
    reasons = dict(NOT_APPLICABLE_CHECKS)
    route = None
    if str(task_view.status.value) == "review":
        route = (await verdict_route(db, task_id)).as_dict()
    return ReviewBrief(
        verdict_route=route,
        task_id=task_view.id,
        title=task_view.title,
        status=task_view.status,
        description=task_view.description,
        project=task_view.project,
        acceptance_criteria=[services.row_to_ac(r) for r in ac_rows],
        call_sites=CallSiteSection(status=NOT_APPLICABLE, reason=reasons["call_sites"]),
        ci_run_report=CIRunReportState(
            state=NOT_APPLICABLE, reason=reasons["ci_run_report"]
        ),
        prepass=PrepassState(state=NOT_APPLICABLE, reason=reasons["ci_run_report"]),
        validation=ValidationStanding(
            state=NOT_APPLICABLE, headline=reasons["ci_run_report"]
        ),
        live_check=LiveCheckState(
            state=NOT_APPLICABLE, reason="пробы кода не заявлены"
        ),
        base_merge=BaseMergeState(state=NOT_APPLICABLE, reason=reasons["base_merge"]),
        scope_in=task_view.scope_in,
        scope_out=task_view.scope_out,
        out_of_scope_for_review=task_view.out_of_scope_for_review,
        review_checklist=task_view.review_checklist,
        validation_commands=task_view.validation_commands,
        constraints=task_view.constraints,
        technical_hints=task_view.technical_hints,
        outcome_metric=task_view.outcome_metric,
        outcome_indicator=task_view.outcome_indicator,
        outcome_deadline=task_view.outcome_deadline,
        outcome_revisit_condition=task_view.outcome_revisit_condition,
        redesign_decision=task_view.redesign_decision,
        redesign_rationale=task_view.redesign_rationale,
        agent_fit=task_view.agent_fit,
        evidence_coverage=state_coverage(view),
        review_cycle=task_view.review_cycle,
        submission_generation=task_view.submission_generation,
        sha_check=NOT_APPLICABLE,
        sha_check_reason=reasons["sha_check"],
        latest_submission_summary=await review_evidence.latest_submission_text(
            db, task_id
        ),
        latest_review=task_view.latest_review,
        current_generation_review=state_generation_review(task_row, view),
        result_kind="state",
        state_review=view,
    )
