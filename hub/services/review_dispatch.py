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
from hub.integrations.registry import plugins
from hub.models import RiskClass
from hub.services.model_family import family
from hub.services.project_policy import gate_policy_of, review_dispatch_enabled

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


# Process surfaces (#820). Measured, not guessed: the lite-vs-deep comparison
# of 21.08.2026 found that the cheap profile caught 2 of 7 confirmed findings,
# and BOTH misses were of one kind — a pytest collector that outlived its
# timeout, and a collection run against whatever branch the shared workspace
# happened to be on. #509 had already produced the same shape twice: an
# orphaned validation command and its unbounded output buffer.
#
# These defects are invisible to reading a diff. You have to know how the
# process behaves after the await is cancelled, and who else writes to that
# workspace. So the surfaces where they live buy the expensive profile
# regardless of risk class — the class says how bad a mistake would be, this
# says how likely one is to hide from a single reader.
#
# The list is a filter against a KNOWN class, never a guarantee: a process
# defect written in words nobody listed here walks straight through. It gets
# extended every time one reaches production.
_PROCESS_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "запуск подпроцессов",
        (
            "create_subprocess_exec",
            "create_subprocess_shell",
            "subprocess.run",
            "subprocess.Popen",
            "Popen(",
            ".communicate(",
        ),
    ),
    (
        "таймауты и отмена",
        ("wait_for(", "asyncio.timeout", "CancelledError", "TimeoutError"),
    ),
    (
        "воркспейс и ветки",
        (
            "workspace_path",
            "worktree",
            "checkout",
            "branch_diff",
            "fetch_base",
            "head_sha",
        ),
    ),
    (
        "конкурентный доступ",
        ("asyncio.Lock", "asyncio.Semaphore", "threading.", "asyncio.gather"),
    ),
)

_COMMENT_PREFIXES = ("#", '"""', "'''", "*", "//")


def _is_comment(code: str) -> bool:
    """A line that only comments or documents. Markers there are talk, not code."""
    stripped = code.strip()
    return not stripped or stripped.startswith(_COMMENT_PREFIXES)


def process_surface_reasons(diff: str) -> list[str]:
    """Which process surfaces this diff ADDS code on (#820).

    Only added lines count, and only outside comments: a diff that merely
    mentions ``wait_for`` in a docstring — as this very module does — must not
    buy the expensive profile, or 'deep' quietly becomes the default and the
    saving #807 exists for is gone.
    """
    reasons: list[str] = []
    for group, markers in _PROCESS_SURFACES:
        hits: list[str] = []
        for raw in diff.splitlines():
            if not raw.startswith("+") or raw.startswith("+++"):
                continue
            code = raw[1:]
            if _is_comment(code):
                continue
            # A marker inside a trailing comment is talk too.
            code = code.split("#", 1)[0]
            for marker in markers:
                if marker in code and marker not in hits:
                    hits.append(marker)
        if hits:
            reasons.append(f"{group}: {', '.join(sorted(hits))}")
    return reasons


# Which risks buy the expensive harness (#827). The catalogue mixes two
# different things: some risks are about the code and its behaviour, others
# about the statement and the product. A multi-agent code review answers the
# first kind and cannot answer the second.
#
# Measured on the first live dispatch (#818, 21.08.2026): the run went deep
# because the task honestly declared a high risk reading "the daily message
# turns into noise and devalues the bot". Dogfooding answers that; reading
# the diff does not. We paid for a harness that had nothing to say.
_TECHNICAL_RISK_KINDS = frozenset(
    {
        "security",
        "breaking_change",
        "data_migration",
        "performance",
        "unknown_unknowns",
    }
)
_PRODUCT_RISK_KINDS = frozenset({"ambiguous_requirements", "large_scope", "other"})


def _risk_profile_reason(risks: Any) -> str | None:
    """Why the declared risks buy deep, or None when they do not (#827).

    Two rules, and the asymmetry between them is deliberate:

    * ``kind=security`` buys deep at ANY severity — unchanged from #807,
      because a security risk somebody rated 'low' is still a security risk.
    * a ``high`` severity buys deep only for TECHNICAL kinds. A product or
      statement risk stays with the class: it is not that such a task is
      safe, it is that this particular instrument cannot read it.

    A kind nobody recognises counts as technical at high severity. Not
    knowing what a risk is must never be the cheap answer (#582) — and it
    also closes the obvious way around the rule.
    """
    if not isinstance(risks, list):
        return None
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        kind = str(risk.get("kind") or "").strip()
        severity = str(risk.get("severity") or "").strip()
        if kind == "security":
            return "заявлен риск security"
        if severity != "high":
            continue
        if kind in _TECHNICAL_RISK_KINDS:
            return f"заявлен технический риск high: {kind}"
        if kind not in _PRODUCT_RISK_KINDS:
            return f"заявлен риск high с нераспознанным видом: {kind or 'не указан'}"
    return None


def pick_review_profile(
    task: dict[str, Any], diff: str | None = None
) -> tuple[str, list[str]]:
    """Which review profile this submission deserves, and why (#807, #820).

    Returns ``(profile, reasons)``. The reasons exist because "why was this
    reviewed cheaply" and "why did this cost a full harness run" are both
    questions somebody asks later, and a bare profile name answers neither.

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
        return DEEP, ["ревью запрошено человеком"]

    # #820: the diff decides before the class does. A missing diff is not a
    # harmless one — the same rule the ladder uses for "class not computed".
    if diff is None:
        return DEEP, ["дифф сдачи прочитать не удалось"]
    surfaces = process_surface_reasons(diff)
    if surfaces:
        return DEEP, [f"процессная поверхность — {r}" for r in surfaces]
    try:
        risks = json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    risk_reason = _risk_profile_reason(risks)
    if risk_reason:
        return DEEP, [risk_reason]
    raw_class = (task.get("risk_class") or "").strip()
    if not raw_class:
        return DEEP, ["класс риска не посчитан"]
    try:
        risk_class = RiskClass(raw_class)
    except ValueError:
        # An unreadable class is an unknown class, and unknown means deep.
        return DEEP, [f"класс риска нечитаем: {raw_class}"]
    order = list(RiskClass)
    if order.index(risk_class) >= order.index(_DEEP_FROM_CLASS):
        return DEEP, [f"класс риска {risk_class.value}"]
    return LITE, [f"класс риска {risk_class.value}, процессных поверхностей нет"]


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


async def _submission_diff(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> str | None:
    """The submitted branch diff, or None when it cannot be read (#820).

    None is a real answer, not an error to swallow: the caller turns it into
    the expensive profile rather than into a quiet lite run over code nobody
    looked at.
    """
    try:
        from hub import services

        ctx = await services.project_git_context(db, task_id)
        workspace = ctx.get("repo")
        base = ctx.get("base_branch") or config.PAIR_BASE_BRANCH
        if not workspace or not branch:
            return None
        return await plugins.git_ops.branch_diff(workspace, base, branch)
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not read the diff of task #%s: %s", task_id, exc)
        return None


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
    # #805: one reader, and it answers "call a reviewer?" — not "who signs
    # the verdict?". Those were the same question only because they shared a
    # key, which forced the hub's own project to choose between no review
    # and no human.
    if not review_dispatch_enabled(gate_policy_of(project)):
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
    # #820: the profile is decided against the SUBMITTED diff, not against the
    # areas the author declared — self-assessment cannot exempt work from
    # oversight (#582). An unreadable diff buys deep, it does not excuse it.
    diff = await _submission_diff(db, task_id, branch)
    profile, profile_reasons = pick_review_profile(task, diff)
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
    # "deep" on its own is not reviewable in hindsight; the reason is (#820).
    if profile_reasons:
        profile_note += " — " + "; ".join(profile_reasons)
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
            "profile_reasons": profile_reasons,
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
            if isinstance(total, int) and total >= 0:
                # #828: keep the number, not just the complaint about it. The
                # economics of the practice were being computed from the
                # harness's own claim; the billed figure was fetched here and
                # dropped on the floor.
                await repo.set_machine_review_provider_tokens(
                    db, task_id, dispatch["submission_generation"], total
                )
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
