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
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from hub.db import fetchall
from hub import config
from hub import repository as repo
from hub.models import ReviewVerdict, RiskClass, TaskReviewVerdict
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


def _mentions_security(findings: list[dict]) -> bool:
    for f in findings:
        blob = " ".join(
            str(f.get(k, "")) for k in ("category", "title", "severity")
        ).lower()
        if "security" in blob or "безопасн" in blob:
            return True
    return False


async def maybe_auto_verdict(db: aiosqlite.Connection, task_id: int) -> bool:
    """Issue APPROVED for the current submission when policy and facts allow.

    Called after a machine-review report lands. Returns True when the
    verdict was recorded. Silent refusals mean "the human gate stands,
    exactly as today"; TRIGGERS are never silent — they write an alert and
    a ``verdict_escalated`` event naming the reason.
    """
    # Kill-switch: the same global lever as the DoR autopilot (#744) — off
    # (or any unknown value) restores today's behavior everywhere.
    mode = (config.AUTO_APPROVE_MAX_CLASS or "off").strip().lower()
    if mode not in {"r0", "r1"}:
        return False

    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    if task.get("status") != "review" or task.get("review_job_id"):
        return False
    generation = task.get("submission_generation") or 0
    if generation == 0:
        return False

    # WHERE automation is allowed: the project's own policy, set by a human
    # (#743). Every resolution failure refuses toward the human gate.
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    try:
        policy = json.loads(project["gate_policy"] or "{}")
    except (ValueError, KeyError):
        return False
    if not isinstance(policy, dict) or policy.get("verdict") != "auto":
        return False

    review_row = await repo.get_latest_machine_review(db, task_id)
    if review_row is None:
        return False
    review = dict(review_row)
    if (review.get("submission_generation") or 0) != generation:
        return False

    confirmed = _finding_dicts(review.get("findings_confirmed"))
    rejected = _finding_dicts(review.get("findings_rejected"))
    unresolved = _finding_dicts(review.get("unresolved"))

    # --- Escalation triggers: never silent -------------------------------
    if _mentions_security(confirmed + rejected + unresolved):
        await _escalate(
            db,
            task_id,
            "security-находка в machine-review (в любом статусе — сам факт "
            "подозрения выводит вердикт к человеку)",
        )
        return False
    budget = config.REVIEW_TOKEN_BUDGET
    if budget and (review.get("tokens_spent") or 0) > budget:
        await _escalate(
            db,
            task_id,
            f"перерасход токен-бюджета ревью: {review.get('tokens_spent')} > "
            f"{budget} — раунд не сошёлся штатно",
        )
        return False
    siblings = await fetchall(
        db,
        "SELECT id, findings_confirmed FROM machine_reviews "
        "WHERE task_id=? AND submission_generation=? AND id != ?",
        (task_id, generation, review["id"]),
    )
    for sibling in siblings:
        if _finding_dicts(sibling["findings_confirmed"]):
            await _escalate(
                db,
                task_id,
                f"расхождение ревьюеров: отчёт #{sibling['id']} этой же сдачи "
                "нёс confirmed-находки, текущий — нет",
            )
            return False

    # --- Clean grounds: silent refusals, the human gate stands -----------
    if confirmed or unresolved or bool(review.get("incomplete")):
        return False
    raw_count = review.get("raw_count") or 0
    proven_usage: int | None = None
    if raw_count < 1:
        # "No candidates at all" is no data, not no findings (harness v7) —
        # unless the provider's billing proves the reviewer worked (#769).
        proven_usage = await _proven_empty_usage(db, task_id, generation, review)
        if proven_usage is None:
            return False
        # Proven work is not proven capability (#835): an empty report may
        # stand in for a review only where a miss costs no more than this
        # reviewer is worth. Refused quietly — no trigger fired, the human
        # gate simply stands — but the reason goes to the feed so the
        # digest (#739) can show what the ceiling actually held back.
        if not _within_proven_empty_ceiling(task):
            ceiling = _proven_empty_ceiling()
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "status",
                (
                    "Автовердикт НЕ вынесен: пустое ревью выше потолка "
                    f"класса. Класс задачи: {task.get('risk_class') or 'не вычислен'}, "
                    f"потолок для пустого ревью: {ceiling.value if ceiling else 'путь закрыт'} "
                    f"(HAIPLANE_PROVEN_EMPTY_MAX_CLASS="
                    f"{config.PROVEN_EMPTY_MAX_CLASS!r}). "
                    f"Работа ревьюера доказана (usage={proven_usage}), способность — нет. "
                    "Вердикт остаётся человеку."
                ),
                author_kind="hub",
            )
            await db.commit()
            return False

    pinned_sha = (task.get("submission_sha") or "").strip()
    if not pinned_sha:
        return False
    ci = await repo.get_ci_run_report(db, task_id, pinned_sha)
    if ci is None or (ci["validation_status"] or "") != VALIDATION_PASS:
        return False

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
    if not current_tip or current_tip != pinned_sha:
        return False

    # The actual diff: inside declared areas, and not raising the class —
    # recomputed here (#550/#583), never trusted from feed prose.
    diff_paths, diff_reason = await _resolve_branch_diff(db, task)
    if diff_paths is None:
        return False
    verdict_state, _undeclared, _detail = _surface_check(task, diff_paths, diff_reason)
    if verdict_state != "ok":
        return False
    from hub.commit_scope import ROUTINE_PATHS
    from hub.models import RiskClass
    from hub.services.risk_class import derive_risk_class

    from hub.services.project_policy import risk_map_for_task

    diff_class, _reasons = derive_risk_class(
        [p for p in diff_paths if p not in ROUTINE_PATHS],
        await risk_map_for_task(db, task_id),
    )
    stored_raw = (task.get("risk_class") or "").strip()
    if diff_class is not None:
        if not stored_raw:
            return False
        order = list(RiskClass)
        if order.index(diff_class) > order.index(RiskClass(stored_raw)):
            return False

    # --- Model diversity (#758): no monoculture reviews -------------------
    from hub.services.model_family import same_family

    implementer_model = (task.get("submission_model") or "").strip()
    reviewer_model = (review.get("model") or "").strip()
    diversity = same_family(implementer_model, reviewer_model)
    if diversity is None:
        # Either declaration missing: absence of data is not diversity —
        # the human gate stands, silently (the raw_count=0 principle).
        return False
    if diversity:
        await _escalate(
            db,
            task_id,
            f"монокультура ревью: код ({implementer_model}) и ревью "
            f"({reviewer_model}) — одно семейство моделей; коррелированные "
            "слепые пятна проходят оба фильтра синхронно",
        )
        return False

    # --- All grounds clean: the policy issues the verdict -----------------
    from hub.services.lifecycle import record_review_verdict

    await record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(verdict=ReviewVerdict.approved, agent=_POLICY_ACTOR),
        self_approved=False,
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        (
            f"Автовердикт APPROVED политикой проекта {project['slug']} "
            f"(verdict=auto). Основания: machine-review #{review['id']} "
            f"(gen {generation}, raw {review.get('raw_count')}, confirmed 0), "
            f"CI {VALIDATION_PASS} на {pinned_sha[:12]}, вершина ветки на "
            "месте, дифф в заявленных областях, класс не вырос. "
            f"Разнородность моделей: код {implementer_model}, ревью "
            f"{reviewer_model}."
            + (
                f" Пустота доказана: usage={proven_usage} (#769)."
                if proven_usage is not None
                else ""
            )
        ),
        author_kind="hub",
    )
    await db.commit()
    log.info(
        "auto-verdict APPROVED for task #%s gen %s (project %s)",
        task_id,
        generation,
        project["slug"],
    )
    return True
