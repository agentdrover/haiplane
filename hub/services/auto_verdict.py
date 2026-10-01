"""Auto-verdict for clean submissions under project policy (#745, F #738).

The last routine human click in the cycle: after a machine review with no
findings and green CI the owner was confirming what automation already
said. This module issues that APPROVED itself — but only where a HUMAN
allowed it (project gate_policy verdict=auto, which the default project
cannot even store, #743), only while the global kill-switch is on, and
only when every ground is clean. Any risk signal escalates to the human
instead: the task simply stays in review — the existing human route, no
new statuses (the needs_decision lesson of 2026-08-20).

Cleanliness is conservative on purpose, and every rule errs toward the
human gate:

- zero confirmed findings, nothing unresolved, harness run complete;
- ``raw_count >= 1`` — a report that surfaced NO candidates at all is
  "no data", not "no findings" (harness v7 shipped 60 such reviews);
  the same principle that keeps risk_class NULL from reading as R0.
  Exception (#769): a raw_count=0 report still counts when the emptiness
  is PROVEN by the provider — the hub itself dispatched the reviewer
  (#757), the dispatch settled as done, and the provider's billed usage
  both clears a floor and agrees with the report's own tokens_spent.
  The first live grok run (claimed 36k, billed 1.47M) fails this check.
  That proof is bounded (#835): billed usage says the reviewer WORKED,
  never that it was ABLE to find, and the reviewer runs on its provider's
  free tier. So the proven-empty path applies only at or below
  ``config.PROVEN_EMPTY_MAX_CLASS``; above it the human keeps the verdict.
  A raw_count >= 1 report is untouched by the ceiling — candidates on the
  table are capability shown, not inferred from a token counter;
- green CI recorded for the PINNED submission_sha (#546/#572);
- the branch tip still stands where it was submitted;
- the actual diff stays inside declared areas and does not raise the
  risk class (#550/#583, recomputed here, not trusted from the feed).
  A missing stored class refuses even when the filtered diff is empty
  (#838) — no signal from the paths is not permission to skip the class.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict, RiskClass, TaskReviewVerdict
from hub.services import gate_grounds as grounds
from hub.services.ci_report import VALIDATION_PASS

log = logging.getLogger(__name__)

_POLICY_ACTOR = "policy"


async def _escalate(db: aiosqlite.Connection, task_id: int, reason: str) -> None:
    """A trigger fired: the verdict stays with the human, visibly."""
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Автовердикт НЕ вынесен — эскалация к владельцу: {reason}. "
        "Задача остаётся в review на человеческий вердикт.",
        author_kind="hub",
    )
    await repo.insert_event(
        db,
        kind="verdict_escalated",
        task_id=task_id,
        actor=_POLICY_ACTOR,
        payload={"reason": reason},
    )
    await db.commit()


def _proven_empty_ceiling() -> RiskClass | None:
    """The highest class an empty review may still auto-approve (#835).

    None closes the proven-empty path entirely — which is what an
    unrecognised value means. A typo in a systemd drop-in must not read as
    "no limit"; the same reasoning that keeps a NULL risk_class from
    reading as R0.
    """
    raw = (config.PROVEN_EMPTY_MAX_CLASS or "").strip().lower()
    try:
        return RiskClass[raw]
    except KeyError:
        return None


def _within_proven_empty_ceiling(task: dict) -> bool:
    """Is this task's OWN class at or below the proven-empty ceiling (#835)?

    A task with no computed class fails: absence of a class is absence of
    data about the blast radius, and an empty review is already absence of
    data about the code. Two unknowns do not make a ground.
    """
    ceiling = _proven_empty_ceiling()
    if ceiling is None:
        return False
    stored = (task.get("risk_class") or "").strip()
    if not stored:
        return False
    try:
        current = RiskClass(stored)
    except ValueError:
        return False
    order = list(RiskClass)
    return order.index(current) <= order.index(ceiling)


async def _reviewer_model(
    db: aiosqlite.Connection, task_id: int, generation: int, review: dict
) -> tuple[str, str]:
    """Which model actually reviewed — the hub's record beats the report (#1008).

    The two sides of the diversity rule are not equally knowable. The hub
    LAUNCHES the reviewer, so for a dispatched run the model is a fact it holds
    (``review_dispatches.model``); the implementer's model is a declaration and
    stays one, because nothing here can observe what wrote the code.

    That asymmetry is the point, not an oversight: a report is free to describe
    itself, and a self-description is exactly what the gate must not lean on
    when the hub has the real answer beside it. A disagreement is not silently
    preferred either way — the second element of the answer carries the alert
    text the DECIDER writes into the task (the route only reads, #1440), and
    the dispatched model is used.
    """
    claimed = (review.get("model") or "").strip()
    dispatch = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    dispatched = (dict(dispatch).get("model") or "").strip() if dispatch else ""
    if not dispatched:
        # No dispatch behind this report: nothing more trustworthy exists, and
        # the declaration is all there is. It still has to be recognisable to
        # count as diversity — see same_family.
        return claimed, ""
    alert = ""
    if claimed and claimed.casefold() != dispatched.casefold():
        alert = (
            f"Отчёт ревью называет модель «{claimed}», а хаб запускал "
            f"«{dispatched}». Для правила разнородности взята модель "
            "диспетчера: её хаб знает, а не со слов отчёта. Сигнал аудиту "
            "(#1008)."
        )
    return dispatched, alert


async def _proven_empty_usage(
    db: aiosqlite.Connection, task_id: int, generation: int, review: dict
) -> int | None:
    """The billed usage proving an empty review worked, or None (#769).

    Proof requires ALL of: the hub's own settled dispatch for this
    generation (#757), a billed total at or above the configured floor,
    and the report's tokens_spent agreeing with that total within the
    same tolerance the sweep uses. Any missing piece is absence of data —
    the #750 rule stands and the caller refuses silently.
    """
    floor = config.EMPTY_REVIEW_MIN_USAGE
    if floor <= 0:
        return None
    dispatch = await repo.get_settled_review_dispatch(db, task_id, generation)
    if dispatch is None:
        return None
    from hub.integrations import cursor_cloud
    from hub.services.review_dispatch import _USAGE_MISMATCH_SHARE

    usage = await cursor_cloud.get_usage(
        dispatch["agent_id"], dispatch["run_id"] or None
    )
    total = ((usage or {}).get("totalUsage") or {}).get("totalTokens")
    if not isinstance(total, int) or total < floor:
        return None
    reported = review.get("tokens_spent")
    if reported is None or abs(total - reported) / total > _USAGE_MISMATCH_SHARE:
        return None
    return total


def _finding_dicts(raw: str | None) -> list[dict]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [f for f in value if isinstance(f, dict)]


def _repeats_note(repeats: dict[str, dict]) -> str:
    """Строка карточки про повторы отложенных: видны, а не спрятаны (#1448)."""
    if not repeats:
        return ""
    named = "; ".join(
        f"«{r['title'][:80]}» отложена до #{r['linked_task_id']}"
        for r in repeats.values()
    )
    return f" — все повторы отложенных, повтор не блокирует: {named}"


# ---------------------------------------------------------------------------
# Стойка автопилота: один вопрос «вынесет ли политика вердикт» (#1440)
# ---------------------------------------------------------------------------
#
# Раньше ответ существовал только как последовательность проверок внутри
# maybe_auto_verdict, и показать его, не повторив проверки, было нельзя —
# а повтор разъезжается с решателем в сторону «автопилот сделает», то есть
# в сторону лжи. Поэтому проверки живут здесь, в ``autopilot_stance``, и
# читает их и решатель (он поверх ответа действует), и маршрут вердикта
# (hub/services/verdict_route.py), который только показывает. Порядок
# проверок — прежний, до слова: исход решателя не изменился.
#
# ``autopilot_stance`` ничего не пишет. Всё, что решатель писал по ходу
# проверок (громкая эскалация, строка в ленту, сигнал аудиту о расхождении
# моделей), стойка возвращает данными, а пишет maybe_auto_verdict.

OUTCOME_APPROVE = "approve"
OUTCOME_REFUSE = "refuse"
OUTCOME_ESCALATE = "escalate"

CODE_APPROVE = "clean_delegated"
CODE_KILL_SWITCH = "kill_switch_off"
CODE_NOT_IN_REVIEW = "not_in_review"
CODE_REVIEW_RUNNING = "review_running"
CODE_NO_SUBMISSION = "no_submission"
CODE_PROJECT_UNRESOLVED = "project_unresolved"
CODE_POLICY_UNREADABLE = "policy_unreadable"
CODE_NOT_DELEGATED = "verdict_not_delegated"
CODE_NO_REPORT = "no_report"
CODE_REPORT_STALE = "report_stale"
CODE_ESCALATION = "escalation"
CODE_UNCLEAN = "unclean_report"
CODE_NO_DATA = "no_data_report"
CODE_ABOVE_CEILING = "above_proven_empty_ceiling"
CODE_NO_PIN = "no_pinned_sha"
CODE_CI_NOT_GREEN = "ci_not_green"
CODE_TIP_MOVED = "branch_tip_moved"
CODE_DIFF_UNREADABLE = "diff_unreadable"
CODE_OUTSIDE_AREAS = "diff_outside_areas"
CODE_CLASS_MISSING = "risk_class_missing"
CODE_CLASS_RAISED = "risk_class_raised"
CODE_MODEL_UNDECLARED = "model_undeclared"

#: Наблюдения, которые стойка не делала, когда её спросили без сети.
PENDING_BRANCH = "branch"
PENDING_PROVIDER_USAGE = "provider_usage"


@dataclass(frozen=True)
class AutoStance:
    """Что автопилот сделает с текущей сдачей — и почему."""

    outcome: str
    code: str
    reason: str = ""
    #: Строка в ленту, которую решатель пишет при тихом, но названном отказе.
    feed_note: str = ""
    #: Сигналы аудиту, найденные по пути; решатель пишет их при любом исходе.
    audit_alerts: tuple[str, ...] = ()
    #: Проверки, требующие сети (ветка, провайдер), которых не делали:
    #: ответ верен «если они пройдут». Пусто там, где решатель.
    pending: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Ctx:
    task: dict
    project: Any
    generation: int
    observe: bool
    review: dict = field(default_factory=dict)
    confirmed: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    repeats: dict[str, dict] = field(default_factory=dict)
    proven_usage: int | None = None
    pinned_sha: str = ""
    implementer_model: str = ""
    reviewer_model: str = ""
    audit: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)


def _refuse_with(
    code: str, reason: str, ctx: _Ctx | None = None, *, note: str = ""
) -> AutoStance:
    return AutoStance(
        OUTCOME_REFUSE,
        code,
        reason,
        feed_note=note,
        audit_alerts=tuple(ctx.audit) if ctx else (),
        pending=tuple(ctx.pending) if ctx else (),
    )


def _escalate_with(code: str, reason: str, ctx: _Ctx) -> AutoStance:
    return AutoStance(
        OUTCOME_ESCALATE,
        code,
        reason,
        audit_alerts=tuple(ctx.audit),
        pending=tuple(ctx.pending),
    )


async def _scope_stage(
    db: aiosqlite.Connection, task_id: int, observe: bool
) -> AutoStance | _Ctx:
    """WHERE automation is allowed: the global switch and the project policy (#743)."""
    # Kill-switch: the same global lever as the DoR autopilot (#744) — off
    # (or any unknown value) restores today's behavior everywhere.
    mode = (config.AUTO_APPROVE_MAX_CLASS or "off").strip().lower()
    if mode not in {"r0", "r1"}:
        return _refuse_with(
            CODE_KILL_SWITCH,
            "автовердикт выключен на сервере (HAIPLANE_AUTO_APPROVE_MAX_CLASS)",
        )
    row = await repo.get_task(db, task_id)
    if row is None:
        return _refuse_with(CODE_NOT_IN_REVIEW, "задачи нет")
    task = dict(row)
    if task.get("status") != "review":
        return _refuse_with(CODE_NOT_IN_REVIEW, "задача не на ревью")
    if task.get("review_job_id"):
        return _refuse_with(CODE_REVIEW_RUNNING, "ревью этой сдачи ещё идёт")
    generation = task.get("submission_generation") or 0
    if generation == 0:
        return _refuse_with(CODE_NO_SUBMISSION, "у задачи нет закреплённой сдачи")
    # Every resolution failure refuses toward the human gate.
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return _refuse_with(CODE_PROJECT_UNRESOLVED, "проект задачи не определён")
    try:
        policy = json.loads(project["gate_policy"] or "{}")
    except (ValueError, KeyError):
        return _refuse_with(CODE_POLICY_UNREADABLE, "политика проекта нечитаема")
    # #1151: делегирование, а не одна строка. Проект, отдавший вердикт
    # СТЮАРДУ, не забирал его у автопилота — он добавил второго судью на
    # грязный путь. Автовердикт по-прежнему закрывает чистые сдачи, иначе
    # перевод проекта на стюарда тихо вернул бы человеку всё, что раньше
    # проходило само, и заметно это стало бы по очереди, а не по отказу.
    from hub.services.project_policy import verdict_is_delegated

    if not verdict_is_delegated(policy):
        return _refuse_with(CODE_NOT_DELEGATED, "вердикт проекта не делегирован")
    return _Ctx(task=task, project=project, generation=generation, observe=observe)


async def _loud_grounds(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    """Escalation triggers: never silent.

    Условия живут в hub/services/gate_grounds.py (#1147): те же пять
    оснований обязан соблюдать стюард, когда применяет вердикт, и два
    описания одного правила разъехались бы в сторону мягкого.
    """
    review = ctx.review
    confirmed = _finding_dicts(review.get("findings_confirmed"))
    rejected = _finding_dicts(review.get("findings_rejected"))
    unresolved = _finding_dicts(review.get("unresolved"))
    ctx.confirmed, ctx.unresolved = confirmed, unresolved
    for ground in (
        grounds.security_ground(confirmed, rejected, unresolved),
        grounds.token_budget_ground(
            review.get("tokens_spent"), config.REVIEW_TOKEN_BUDGET
        ),
        await grounds.sibling_mismatch_ground(
            db, task_id, ctx.generation, review["id"]
        ),
    ):
        if ground:
            return _escalate_with(CODE_ESCALATION, ground, ctx)
    return None


async def _empty_report_stage(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    """``raw_count`` 0 is "no data" unless the provider's billing proves work (#769)."""
    # "No candidates at all" is no data, not no findings (harness v7) —
    # unless the provider's billing proves the reviewer worked (#769).
    if not ctx.observe:
        # Биллинг провайдера — сеть; без неё ответ «если докажется».
        ctx.pending.append(PENDING_PROVIDER_USAGE)
    else:
        ctx.proven_usage = await _proven_empty_usage(
            db, task_id, ctx.generation, ctx.review
        )
        if ctx.proven_usage is None:
            return _refuse_with(
                CODE_NO_DATA, "отчёт без единого кандидата — это «нет данных»", ctx
            )
    # Proven work is not proven capability (#835): an empty report may
    # stand in for a review only where a miss costs no more than this
    # reviewer is worth. Refused quietly — no trigger fired, the human
    # gate simply stands — but the reason goes to the feed so the
    # digest (#739) can show what the ceiling actually held back.
    if not _within_proven_empty_ceiling(ctx.task):
        ceiling = _proven_empty_ceiling()
        note = (
            "Автовердикт НЕ вынесен: пустое ревью выше потолка "
            f"класса. Класс задачи: {ctx.task.get('risk_class') or 'не вычислен'}, "
            f"потолок для пустого ревью: {ceiling.value if ceiling else 'путь закрыт'} "
            f"(HAIPLANE_PROVEN_EMPTY_MAX_CLASS="
            f"{config.PROVEN_EMPTY_MAX_CLASS!r}). "
            f"Работа ревьюера доказана (usage={ctx.proven_usage}), способность — нет. "
            "Вердикт остаётся человеку."
        )
        return _refuse_with(
            CODE_ABOVE_CEILING,
            "пустое ревью выше потолка класса: способность ревьюера не доказана",
            ctx,
            note=note,
        )
    return None


async def _report_stage(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    review_row = await repo.get_latest_machine_review(db, task_id)
    if review_row is None:
        return _refuse_with(CODE_NO_REPORT, "отчёта машинного ревью нет")
    ctx.review = dict(review_row)
    if (ctx.review.get("submission_generation") or 0) != ctx.generation:
        return _refuse_with(
            CODE_REPORT_STALE, "отчёт относится не к текущей сдаче", ctx
        )
    loud = await _loud_grounds(db, task_id, ctx)
    if loud is not None:
        return loud
    # --- Clean grounds: silent refusals, the human gate stands -----------
    #
    # Which sections owe an account lives in gate_grounds (#1170), for the
    # same reason the loud five do: the steward has to refuse where this
    # refuses, and it did not — ``unresolved`` was invisible to it entirely.
    # Silent here and named there is fine; two different lists would not be.
    # Повтор находки, осознанно отложенной до НЕдоставленной задачи, отчёта
    # не требует (#1448): тот же предикат читает стюард, и он же показан в
    # карточке. Security и прочие границы держит сам предикат.
    ctx.repeats = await grounds.deferred_repeats(db, task_id, ctx.confirmed)
    if grounds.unattended_blockers(
        ctx.confirmed,
        ctx.unresolved,
        bool(ctx.review.get("incomplete")),
        ctx.repeats,
    ):
        return _refuse_with(
            CODE_UNCLEAN,
            "в отчёте есть находки, нерешённое или он неполный",
            ctx,
        )
    if (ctx.review.get("raw_count") or 0) < 1:
        return await _empty_report_stage(db, task_id, ctx)
    return None


async def _observed_facts_stage(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    """CI on the pinned sha, the tip where it was submitted, the diff in its areas."""
    task = ctx.task
    ctx.pinned_sha = (task.get("submission_sha") or "").strip()
    if not ctx.pinned_sha:
        return _refuse_with(CODE_NO_PIN, "коммит сдачи не закреплён", ctx)
    ci = await repo.get_ci_run_report(db, task_id, ctx.pinned_sha)
    if ci is None or (ci["validation_status"] or "") != VALIDATION_PASS:
        return _refuse_with(
            CODE_CI_NOT_GREEN, "CI на закреплённом коммите не зелёный", ctx
        )
    if not ctx.observe:
        # Вершина и дифф — сеть. Класс риска от них не зависит и читается.
        ctx.pending.append(PENDING_BRANCH)
        if not (task.get("risk_class") or "").strip():
            return _refuse_with(CODE_CLASS_MISSING, "класс риска не вычислен", ctx)
        return None
    return await _branch_stage(db, task_id, ctx)


async def _branch_stage(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    task = ctx.task
    # The tip must still stand where it was submitted — an auto-approval of
    # commits nobody reviewed is exactly the hole #572 closed for humans.
    from hub.services.lifecycle import (
        _resolve_branch_diff,
        _surface_check,
        resolve_branch_tip,
    )

    current_tip, _tip_reason = await resolve_branch_tip(
        db, task_id, task.get("branch") or ""
    )
    if not current_tip or current_tip != ctx.pinned_sha:
        return _refuse_with(CODE_TIP_MOVED, "вершина ветки не там, где её сдали", ctx)
    # The actual diff: inside declared areas, and not raising the class —
    # recomputed here (#550/#583), never trusted from feed prose.
    diff_paths, diff_reason = await _resolve_branch_diff(db, task)
    if diff_paths is None:
        return _refuse_with(CODE_DIFF_UNREADABLE, "дифф ветки не прочитан", ctx)
    verdict_state, _undeclared, _detail = _surface_check(task, diff_paths, diff_reason)
    if verdict_state != "ok":
        return _refuse_with(CODE_OUTSIDE_AREAS, "дифф вышел за заявленные области", ctx)
    from hub.commit_scope import ROUTINE_PATHS
    from hub.services.project_policy import risk_map_for_task
    from hub.services.risk_class import derive_risk_class

    diff_class, _reasons = derive_risk_class(
        [p for p in diff_paths if p not in ROUTINE_PATHS],
        await risk_map_for_task(db, task_id),
    )
    stored_raw = (task.get("risk_class") or "").strip()
    if not stored_raw:
        note = (
            "Автовердикт НЕ вынесен: класс риска задачи не вычислен. "
            "Без класса сверка радиуса сдачи невозможна, даже когда дифф "
            "сам сигнала не дал. Вердикт остаётся человеку."
        )
        return _refuse_with(
            CODE_CLASS_MISSING, "класс риска не вычислен", ctx, note=note
        )
    if diff_class is not None:
        order = list(RiskClass)
        if order.index(diff_class) > order.index(RiskClass(stored_raw)):
            return _refuse_with(CODE_CLASS_RAISED, "дифф поднял класс риска", ctx)
    return None


async def _independence_stage(
    db: aiosqlite.Connection, task_id: int, ctx: _Ctx
) -> AutoStance | None:
    # --- Reviewer independence (#728): not the author's own report --------
    #
    # The diversity rule below asks whether the reviewer's MODEL differs from
    # the implementer's; nothing asked whether the PRINCIPAL did. So the
    # implementer could report on their own work under another model
    # declaration and the policy would sign it off as independent — the very
    # bypass the human path closed in #318. Escalated, not refused in silence:
    # this module keeps silence for absent data, and a self-review is a
    # positive fact about the report in hand.
    self_reviewed = bool(ctx.review.get("self_reviewed"))
    solo = config.REVIEW_SELF_APPROVE == "allow"
    self_ground = grounds.self_review_ground(self_reviewed, solo)
    if self_ground:
        return _escalate_with(CODE_ESCALATION, self_ground, ctx)

    # --- Model diversity (#758): no monoculture reviews -------------------
    from hub.services.model_family import same_family

    ctx.implementer_model = (ctx.task.get("submission_model") or "").strip()
    ctx.reviewer_model, alert = await _reviewer_model(
        db, task_id, ctx.generation, ctx.review
    )
    if alert:
        ctx.audit.append(alert)
    diversity = same_family(ctx.implementer_model, ctx.reviewer_model)
    if diversity is None:
        # Either declaration missing: absence of data is not diversity —
        # the human gate stands, silently (the raw_count=0 principle).
        return _refuse_with(
            CODE_MODEL_UNDECLARED,
            "модель исполнителя или ревьюера не объявлена: "
            "отсутствие данных не есть разнообразие",
            ctx,
        )
    mono_ground = grounds.monoculture_ground(ctx.implementer_model, ctx.reviewer_model)
    if mono_ground:
        return _escalate_with(CODE_ESCALATION, mono_ground, ctx)
    return None


async def autopilot_stance(
    db: aiosqlite.Connection, task_id: int, *, observe: bool = True
) -> AutoStance:
    """Вынесет ли политика вердикт текущей сдачи — читатель, не писатель (#1440).

    ``observe=False`` не ходит в сеть (ветка, биллинг провайдера): такой
    ответ называет эти проверки в ``pending`` и верен «если они пройдут».
    Решатель всегда спрашивает с ``observe=True``.
    """
    scoped = await _scope_stage(db, task_id, observe)
    if isinstance(scoped, AutoStance):
        return scoped
    ctx = scoped
    for stage in (_report_stage, _observed_facts_stage, _independence_stage):
        stopped = await stage(db, task_id, ctx)
        if stopped is not None:
            return stopped
    return AutoStance(
        OUTCOME_APPROVE,
        CODE_APPROVE,
        "чистая сдача, вердикт делегирован",
        audit_alerts=tuple(ctx.audit),
        pending=tuple(ctx.pending),
        facts={
            "project_slug": ctx.project["slug"],
            "review_id": ctx.review["id"],
            "generation": ctx.generation,
            "raw_count": ctx.review.get("raw_count"),
            "confirmed": len(ctx.confirmed),
            "repeats_note": _repeats_note(ctx.repeats),
            "pinned_sha": ctx.pinned_sha,
            "implementer_model": ctx.implementer_model,
            "reviewer_model": ctx.reviewer_model,
            "proven_usage": ctx.proven_usage,
            "self_reviewed": bool(ctx.review.get("self_reviewed")),
        },
    )


async def maybe_auto_verdict(db: aiosqlite.Connection, task_id: int) -> bool:
    """Issue APPROVED for the current submission when policy and facts allow.

    Called after a machine-review report lands. Returns True when the
    verdict was recorded. Silent refusals mean "the human gate stands,
    exactly as today"; TRIGGERS are never silent — they write an alert and
    a ``verdict_escalated`` event naming the reason.

    Что решать — отвечает ``autopilot_stance`` (его же читает маршрут
    вердикта, #1440); здесь только то, что делают с ответом.
    """
    stance = await autopilot_stance(db, task_id)
    for alert in stance.audit_alerts:
        await repo.add_task_update(
            db, task_id, "hub", "alert", alert, author_kind="hub"
        )
        await db.commit()
    if stance.outcome == OUTCOME_ESCALATE:
        await _escalate(db, task_id, stance.reason)
        return False
    if stance.outcome != OUTCOME_APPROVE:
        if stance.feed_note:
            await repo.add_task_update(
                db, task_id, "hub", "status", stance.feed_note, author_kind="hub"
            )
            await db.commit()
        return False
    return await _approve(db, task_id, stance.facts)


async def _approve(db: aiosqlite.Connection, task_id: int, facts: dict) -> bool:
    """All grounds clean: the policy issues the verdict."""
    from hub.services.lifecycle import record_review_verdict

    # Solo mode is the only way past the independence check, and it does not
    # make a self-review independent — it makes it permitted. The verdict is
    # stored as self-approved so the audit trail says which of the two it was
    # (#434).
    await record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent=_POLICY_ACTOR),
        self_approved=facts["self_reviewed"],
    )
    proven = facts["proven_usage"]
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        (
            f"Автовердикт APPROVED политикой проекта {facts['project_slug']} "
            f"(verdict=auto). Основания: machine-review #{facts['review_id']} "
            f"(gen {facts['generation']}, raw {facts['raw_count']}, "
            f"confirmed {facts['confirmed']}{facts['repeats_note']}), "
            f"CI {VALIDATION_PASS} на {facts['pinned_sha'][:12]}, вершина ветки на "
            "месте, дифф в заявленных областях, класс не вырос. "
            f"Разнородность моделей: код {facts['implementer_model']}, ревью "
            f"{facts['reviewer_model']}."
            + (
                f" Пустота доказана: usage={proven} (#769)."
                if proven is not None
                else ""
            )
        ),
        author_kind="hub",
    )
    await db.commit()
    log.info(
        "auto-verdict APPROVED for task #%s gen %s (project %s)",
        task_id,
        facts["generation"],
        facts["project_slug"],
    )
    return True
