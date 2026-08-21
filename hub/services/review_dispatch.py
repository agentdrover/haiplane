"""Hub-dispatched cross-model reviews (#757, feature #738).

Until now the machine review was launched by the implementer (or its
orchestrator) — the reviewed party chose its reviewer. This module moves
the call to the hub: a submission in a ``verdict=auto`` project queues a
Cursor cloud agent on the task branch, with a model whose FAMILY differs
from the implementer's declaration (#758), the hub's own MCP inline under
the dedicated reviewer principal, and explicit prohibitions on writing.

Everything is best-effort by contract: a failed dispatch alerts once and
changes nothing — the verdict simply stays with the human, exactly as it
would have without this module. The poller walks active dispatches; a run
that finished without a report for its generation fails LOUDLY (a stamp
factory must never look like coverage — the cursor_cloud lesson), and a
report whose token count disagrees with the provider's own usage numbers
is flagged: cross-checking against the provider closes the #750 class of
stamps with data instead of discipline.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.models import RiskClass
from hub.services.model_family import family

log = logging.getLogger(__name__)

# Real ids from GET /v1/models (checked live 2026-08-20). Order = preference;
# the first whose family differs from the implementer's wins.
_REVIEW_MODEL_PREFERENCES: tuple[str, ...] = (
    "grok-4.6",
    "gpt-5.3-codex",
    "gemini-3.1-pro",
    "claude-sonnet-5",
)

_TERMINAL_RUN_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}

# Usage-vs-report tolerance: the harness counts tokens its own way, the
# provider counts billing tokens — a mismatch beyond this share (or a report
# claiming NO tokens while the provider billed some) is flagged to the audit.
_USAGE_MISMATCH_SHARE = 0.25


def pick_review_model(implementer_model: str) -> str:
    """The reviewer model: config override first, else the first preference
    from another family. Falls back to the first preference when the
    implementer is undeclared — the diversity rule (#758) will keep the
    verdict with the human in that case anyway."""
    override = (config.CURSOR_REVIEW_MODEL or "").strip()
    impl_family = family(implementer_model)
    if override:
        return override
    for candidate in _REVIEW_MODEL_PREFERENCES:
        if not impl_family or family(candidate) != impl_family:
            return candidate
    return _REVIEW_MODEL_PREFERENCES[0]


LITE = "lite"
DEEP = "deep"

# Risk classes at or above this one never get the cheap profile.
_DEEP_FROM_CLASS = RiskClass.r3


def pick_review_profile(task: dict[str, Any]) -> str:
    """Which review profile this submission deserves (#807).

    deep is the multi-agent harness; lite is a single pass over the branch
    diff under a token ceiling. The rule leans toward deep on every kind of
    ignorance:

    * a human who pressed "request machine review" asked for the real thing;
    * a high or security risk is exactly what the expensive harness is for;
    * an UNCOMPUTED risk class is not a low one (#582) — an unknown path is
      not cheaper than a known-harmless one, and treating a missing class as
      lite would let any task skip the harness by never being classified.

    Everything else is ordinary work inside known contracts, and paying
    434k tokens per confirmed finding for it is what made "review every
    submission" unaffordable in the first place.
    """
    if (task.get("machine_review_override") or "").strip() == "require":
        return DEEP
    try:
        risks = json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    for risk in risks:
        if isinstance(risk, dict) and (
            risk.get("severity") == "high" or risk.get("kind") == "security"
        ):
            return DEEP
    raw_class = (task.get("risk_class") or "").strip()
    if not raw_class:
        return DEEP
    try:
        risk_class = RiskClass(raw_class)
    except ValueError:
        # An unreadable class is an unknown class, and unknown means deep.
        return DEEP
    order = list(RiskClass)
    if order.index(risk_class) >= order.index(_DEEP_FROM_CLASS):
        return DEEP
    return LITE


def _review_prompt(task_id: int, branch: str, model_id: str, profile: str) -> str:
    common = (
        f"Ты — независимый код-ревьюер задачи #{task_id} хаба OpenClaw "
        f"(ветка {branch}). Строгие правила: НИЧЕГО не коммить, не пушить и "
        "не менять — только читать код и запускать проверки. "
    )
    if profile == LITE:
        budget = config.REVIEW_LITE_TOKEN_BUDGET
        return (
            common + "Это ЛЁГКОЕ ревью: один проход, бюджет "
            f"{budget} токенов. Порядок: "
            f"1) hub_get_review_brief(task_id={task_id}) — предмет ревью; "
            "2) прочитай ДИФФ ветки к базовой (git diff) и только его — "
            "не исследуй репозиторий целиком, контекст берётся из диффа; "
            "3) один проход по изменённым файлам: ищи дефекты корректности, "
            "потерянные граничные случаи, несоответствие заявленным AC; "
            f"4) сдай hub_submit_machine_review(task_id={task_id}, "
            "harness_skill='lite-diff-review', ...) с реальными raw_count, "
            f"находками, tokens_spent и model='{model_id}'. "
            "ЧЕСТНОСТЬ ОХВАТА: если дифф не помещается в бюджет — сдавай "
            "incomplete=true и перечисли непрочитанные файлы в "
            "lost_dimensions. Ноль находок при обрезанном диффе — это "
            "«не проверено», а не «чисто»; выдать одно за другое хуже, чем "
            "не найти ничего. Вердикт НЕ выноси — он не твой."
        )
    return (
        common + "Порядок: "
        "1) hub_get_skill('multi-agent-review') и работай по нему; "
        f"2) hub_get_review_brief(task_id={task_id}) — предмет ревью; "
        "3) исполни фазы измерений и адъюдикации ЧЕСТНО — отчёт без "
        "исполнения запрещён скиллом v8 и виден серверу; "
        f"4) сдай hub_submit_machine_review(task_id={task_id}, ...) с "
        f"реальными raw_count, находками, tokens_spent и model='{model_id}'. "
        "Вердикт НЕ выноси — он не твой."
    )


async def maybe_dispatch_review(db: aiosqlite.Connection, task_id: int) -> bool:
    """Queue a cloud reviewer for a fresh submission when policy allows.

    Called after submit_for_review commits. Returns True when a dispatch
    was recorded. Every refusal is either silent (policy does not ask for
    it) or a single visible alert (policy asked, the call failed).
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    if task.get("status") != "review" or task.get("review_job_id"):
        return False
    generation = task.get("submission_generation") or 0
    branch = (task.get("branch") or "").strip()
    if generation == 0 or not branch or not (task.get("submission_sha") or "").strip():
        return False

    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    try:
        policy = json.loads(project["gate_policy"] or "{}")
    except (ValueError, KeyError):
        return False
    if not isinstance(policy, dict) or policy.get("verdict") != "auto":
        return False

    gh_repo = (dict(project).get("repo") or "").strip()
    reviewer_token = (config.CURSOR_REVIEWER_HUB_TOKEN or "").strip()
    if not cursor_cloud.is_configured() or not gh_repo or not reviewer_token:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации "
            "(ключ Cursor API, репозиторий проекта или ревьюер-токен). "
            "Вердикт остаётся человеку (#757).",
        )
        await db.commit()
        return False

    model_id = pick_review_model((task.get("submission_model") or "").strip())
    profile = pick_review_profile(task)
    hub_mcp_url = f"{instance_base_url().rstrip('/')}/mcp"
    created = await cursor_cloud.create_review_agent(
        repo_url=f"https://github.com/{gh_repo}",
        starting_ref=branch,
        model_id=model_id,
        prompt_text=_review_prompt(task_id, branch, model_id, profile),
        hub_mcp_url=hub_mcp_url,
        reviewer_token=reviewer_token,
    )
    agent_info = (created or {}).get("agent") or {}
    run_info = (created or {}).get("run") or {}
    agent_id = agent_info.get("id") or ""
    if not agent_id:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Кросс-модельное ревью НЕ вызвано: Cloud Agents API не принял "
            "запрос (бета могла измениться). Вердикт остаётся человеку; "
            "детали в логе хаба (#757).",
        )
        await db.commit()
        return False

    await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=agent_id,
        run_id=run_info.get("id") or agent_info.get("latestRunId") or "",
        model=model_id,
        profile=profile,
    )
    profile_note = (
        f"профиль {profile} (бюджет {config.REVIEW_LITE_TOKEN_BUDGET} токенов)"
        if profile == LITE
        else f"профиль {profile} (многоагентный харнесс)"
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Кросс-модельное ревью вызвано хабом: модель {model_id} "
        f"(семейство ≠ {task.get('submission_model') or 'не заявлено'}), "
        f"{profile_note}, агент {agent_id}. Отчёт придёт через "
        "hub_submit_machine_review от принципала cursor-cloud-reviewer "
        "(#757, #807).",
    )
    await repo.insert_event(
        db,
        kind="review_dispatched",
        task_id=task_id,
        actor="policy",
        payload={
            "model": model_id,
            "agent_id": agent_id,
            "run_id": run_info.get("id") or "",
            "generation": generation,
            "profile": profile,
        },
    )
    await db.commit()
    log.info(
        "dispatched cross-model review for task #%s gen %s: %s (%s)",
        task_id,
        generation,
        model_id,
        agent_id,
    )
    return True


def instance_base_url() -> str:
    from hub.hub_instance import instance_echo_fields

    return instance_echo_fields().get("base_url") or "https://agenthai.ru"


async def _current_review(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    row = await repo.get_latest_machine_review(db, task_id)
    if row is None:
        return None
    review = dict(row)
    if (review.get("submission_generation") or 0) != generation:
        return None
    return review


async def sweep_review_dispatches(db: aiosqlite.Connection) -> None:
    """Poller pass over active dispatches: settle finished runs.

    - report arrived → cross-check tokens against the provider's usage
      (mismatch is an audit flag, never a mechanical block) → done;
    - run terminal, no report, grace expired → one loud alert → failed;
    - run still going / API unreachable → leave for the next pass.
    """
    for row in await repo.list_active_review_dispatches(db):
        dispatch = dict(row)
        task_id = dispatch["task_id"]
        review = await _current_review(db, task_id, dispatch["submission_generation"])
        if review is not None:
            usage = await cursor_cloud.get_usage(
                dispatch["agent_id"], dispatch["run_id"] or None
            )
            total = ((usage or {}).get("totalUsage") or {}).get("totalTokens")
            reported = review.get("tokens_spent")
            if isinstance(total, int) and total > 0:
                mismatch = reported is None or (
                    abs(total - reported) / total > _USAGE_MISMATCH_SHARE
                )
                if mismatch:
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "alert",
                        f"Отчёт ревью расходится с данными провайдера: "
                        f"tokens_spent={reported}, Cursor usage={total}. "
                        "Сигнал аудиту — сверка по данным, не по дисциплине "
                        "(#757).",
                    )
            await repo.set_review_dispatch_status(db, dispatch["id"], "done")
            await db.commit()
            continue

        run = await cursor_cloud.get_run(dispatch["agent_id"], dispatch["run_id"])
        if run is None:
            continue  # API hiccup or run still unknown — retry next pass
        if (run.get("status") or "").upper() not in _TERMINAL_RUN_STATUSES:
            continue
        grace_rows = await db.execute_fetchall(
            "SELECT 1 FROM review_dispatches WHERE id=? "
            "AND created_at <= datetime('now', ?)",
            (dispatch["id"], f"-{config.CURSOR_REVIEW_GRACE_MINUTES} minutes"),
        )
        if not grace_rows:
            continue
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Кросс-модельное ревью вызвано, но отчёт НЕ сдан: агент "
            f"{dispatch['agent_id']} ({dispatch['model']}) завершил ран со "
            f"статусом {run.get('status')}, machine-review актуальной "
            "генерации отсутствует. Вердикт остаётся человеку (#757).",
        )
        await repo.set_review_dispatch_status(db, dispatch["id"], "failed")
        await db.commit()
