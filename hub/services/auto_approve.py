"""Auto-approval of low-risk drafts (#584, epic #578, F2).

The first real removal of a human gate, taken as a narrow band: R0 (docs,
texts) and R1 (tests, templates, local cleanup) — classes DERIVED from
observable facts (#582), never declared by the author. Everything here is
built around four properties the task demands:

1. THE SWITCH — ``config.AUTO_APPROVE_MAX_CLASS``: 'off' by default, and
   turning it off restores today's behavior in full. Irreversible
   delegation is not delegation, it is abandonment of control.
2. THE REASON IN THE FEED — every auto-approval records which class passed
   and on which features it was derived; without that there is nothing to
   take apart after an incident.
3. HUB AUTHORSHIP — the record is written as the hub (author_kind=hub,
   #559), never as a principal: an auto-approval must not look like a
   human's decision.
4. "NOT COMPUTED" NEVER PASSES — the absence of a class is not low risk.

And one meta-rule: tasks that touch the gates or the ladder itself are
never auto-approved, whatever their class says. The system does not get to
simplify its own rules (see ``LADDER_SURFACES``).
"""

from __future__ import annotations

import logging

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import deserialize_str_list
from hub.models import DoRCheckItem, RiskClass
from hub.services.project_policy import (
    freeze_admission,
    gate_policy_of,
    gate_value_of,
)

log = logging.getLogger(__name__)

# Surfaces that ARE the ladder and the gates: the risk derivation, this very
# module, the lifecycle transitions, the switch itself, auth, and the process
# rules the gates enforce. A task declaring any of these is a change to the
# oversight machinery and stays with the owner even at a low class — matched
# by exact path or prefix against declared affected_areas.
LADDER_SURFACES: tuple[str, ...] = (
    "hub/services/risk_class.py",
    "hub/services/auto_approve.py",
    "hub/services/auto_verdict.py",
    "hub/services/lifecycle.py",
    "hub/config.py",
    "hub/auth.py",
    "hub/mcp_internal_auth.py",
    "docs/agent-context/",
    "docs/repository-rules.md",
    ".github/",
    # #1147: границы самого судьи. Стюард получает право применять вердикт,
    # и суждение, меняющее его собственные предусловия, режим или политику,
    # обязано оставаться у человека по определению — иначе автономия умеет
    # расширять себя. Перечислено путями, а не намерением: границу, которую
    # нельзя перечислить, нельзя и проверить.
    "hub/services/steward_apply.py",
    # #1231: здесь же лежит правило самостоятельного одобрения. Модуль
    # появился после #1147 и в перечень тогда не попал — а именно он
    # решает, уедет ли сдача без человека вовсе. Задача, меняющая это
    # правило, обязана остаться у человека по тому же основанию, по
    # которому здесь стоят остальные модули контура.
    "hub/services/steward_applied.py",
    "hub/services/steward_dispatch.py",
    "hub/services/steward_evidence.py",
    "hub/services/steward_judgement.py",
    "hub/services/steward_shadow.py",
    "hub/services/gate_grounds.py",
    "hub/services/project_policy.py",
)

# #1559, решение владельца 03.10 (вариант Б): выбор профиля ревью читает НЕ весь
# контур, а его подмножество — решатели одобрения и доступа, код, который
# одобряет или пускает без человека. lifecycle.py, config.py, project_policy.py,
# docs/agent-context/, docs/repository-rules.md и .github/ остаются в
# LADDER_SURFACES для автоодобрения, но deep не покупают: замер исполнителя —
# 32 новых deep за 2 недели, в основном из-за них. Каждая запись обязана быть
# буквально элементом LADDER_SURFACES (тест подмножества), список истины один.
APPROVAL_DECIDERS: tuple[str, ...] = (
    "hub/services/auto_verdict.py",
    "hub/services/auto_approve.py",
    "hub/services/steward_apply.py",
    "hub/services/steward_applied.py",
    "hub/services/steward_dispatch.py",
    "hub/services/steward_evidence.py",
    "hub/services/steward_judgement.py",
    "hub/services/steward_shadow.py",
    "hub/services/gate_grounds.py",
    "hub/services/risk_class.py",
    "hub/auth.py",
    "hub/mcp_internal_auth.py",
)

# The classes the switch can name. R2 stays OUT: opening it is #585, and that
# task is conditioned on a measured agreement between the agent reviewer and
# the owner (#522/#527) — a condition set on 31.07.2026 and not yet met. The
# per-project ceiling below can therefore only tighten this band, never widen
# it, which is the only safe direction while the number is missing.
_AUTO_BAND: dict[str, RiskClass] = {"r0": RiskClass.r0, "r1": RiskClass.r1}


def project_ceiling_of(policy: dict) -> RiskClass | None:
    """Потолок класса риска из ``dor_max_class`` проекта; ``None`` — не задан."""
    raw = policy.get("dor_max_class") if isinstance(policy, dict) else None
    return _AUTO_BAND.get(str(raw or "").lower())


def ladder_hits(paths: list[str]) -> list[str]:
    """Какие из путей попадают в ladder-поверхности.

    Принимает ЛЮБОЙ список путей — заявленные области или фактический дифф.
    Разница между ними существенна и решается вызывающим, а не здесь: на
    DoR диффа ещё нет и сверять можно только декларацию, а на применении
    вердикта дифф есть, и верить декларации там значит пропускать задачу,
    объявившую «hub/services/» и поменявшую hub/auth.py (#1147).
    """
    return surface_hits(paths, LADDER_SURFACES)


def surface_hits(paths: list[str], surfaces: tuple[str, ...]) -> list[str]:
    """Пути, совпавшие с поверхностью из списка: точный путь или префикс.

    Единственное место правила совпадения; ``ladder_hits`` и
    ``decider_hits`` отличаются только списком.
    """
    return sorted(p for p in paths if any(p == s or p.startswith(s) for s in surfaces))


def decider_hits(paths: list[str]) -> list[str]:
    """Какие из путей — решатели одобрения (``APPROVAL_DECIDERS``, #1559)."""
    return surface_hits(paths, APPROVAL_DECIDERS)


def _touches_ladder(areas: list[str]) -> list[str]:
    return ladder_hits(areas)


_FREEZE_NOTE_PREFIX = "Автоодобрение отклонено заморозкой проекта (dor=auto): "


async def _note_freeze_refusal(
    db: aiosqlite.Connection, task_id: int, reason: str
) -> None:
    """Строка ленты об отказе заморозки; не повторяется, пока причина та же.

    Функция зовётся при каждой записи готовности, и тот же отказ на каждый
    вызов засорил бы карточку одинаковыми строками.
    """
    content = _FREEZE_NOTE_PREFIX + reason
    for update in reversed(await repo.get_task_updates(db, task_id)):
        if str(update["content"]).startswith(_FREEZE_NOTE_PREFIX):
            if update["content"] == content:
                return
            break
    await repo.add_task_update(db, task_id, "hub", "status", content, author_kind="hub")


async def maybe_auto_approve(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    dor_checks: list[DoRCheckItem] | None = None,
) -> bool:
    """Approve a DoR-passed low-class draft when the switch allows (#584).

    CALLERS, ENUMERATED RATHER THAN CLAIMED (#1164). Two, and the list is
    the whole guarantee — the previous version of this docstring named
    "every path (refine, bulk refine, AC/risk mutations)" while bulk refine
    demonstrably did not arrive, so a coverage check made against this text
    answered "covered" about an uncovered path:

    1. ``refinement._persist_readiness_and_revision`` — THE readiness write
       funnel. Both write paths go through it (the single refine and the
       AC/risk mutations inside their ``_atomic`` block, the bulk refine as
       one pass after the batch commits), which is why the call lives there
       and not at each caller: a third write path would otherwise slip past
       exactly the way bulk refine did.
    2. ``steward_dispatch._order_one_dor_run`` — the poller, before it pays
       for a steward run on a draft the policy would open for free.

    ``_persist_readiness_fields`` one floor below is deliberately NOT a
    caller: the lazy repair in ``get_readiness`` reaches it, and that is a
    read — a card view must not open drafts. Held by
    ``test_reading_a_card_never_opens_a_draft``, not by this paragraph:
    review #313 pointed out that moving the call one floor down left the
    whole suite green, and the measurement agreed (3475 passed).

    ``dor_checks`` are the checks the caller ALREADY computed and gated on
    (#1456). The statement-path record is written from them: this runs under
    the caller's write lock, and a second ``evaluate_dor`` would read the git
    tree there. A caller with no computed checks (the steward poller) passes
    none, and the record is skipped rather than recomputed.

    Runs inside the caller's transaction; returns True when the draft was
    transitioned. Every refusal is silent by design: a draft that does not
    qualify simply keeps waiting for the human, exactly as today. The one
    refusal that is NOT silent is the project freeze (#1594): a draft that
    qualified in every other way and was stopped by the freeze gets a feed
    line with the reason.
    """
    mode = (config.AUTO_APPROVE_MAX_CLASS or "off").strip().lower()
    global_ceiling = _AUTO_BAND.get(mode)
    if global_ceiling is None:
        # 'off' — and also any unknown value: a mistyped switch must fail
        # toward the human gate, never toward silent delegation.
        return False

    row = await repo.get_task(db, task_id)
    if row is None or row["status"] != "draft" or not row["dor_passed"]:
        return False

    raw_class = row["risk_class"]
    if not raw_class:
        # "Not computed" is not low risk (#581) — never auto-approved.
        return False
    try:
        risk = RiskClass(raw_class)
    except ValueError:
        return False

    order = list(RiskClass)

    areas = deserialize_str_list(row["affected_areas"])
    ladder = _touches_ladder(areas)
    if ladder:
        # The system does not simplify its own rules: gate/ladder changes
        # wait for the owner at ANY class.
        return False

    # #744: the policy says WHERE automation is allowed, the class says WHAT
    # is safe, and the global env above stays the kill-switch and the class
    # ceiling. Resolved the same way the git conveyor resolves it — by
    # walking the hierarchy — and every failure mode (no project, unparsable
    # policy, no dor=auto) refuses toward the human gate, never toward auto.
    # The default project cannot even store 'auto' (#743), so the hub's own
    # drafts can never take this path.
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return False
    policy = gate_policy_of(project)
    if gate_value_of(policy, "dor") != "auto":
        return False
    project_slug = project["slug"]

    # #760: the project may lower its own ceiling, never raise it. The global
    # switch stays the upper bound and the kill-switch — a project that asks
    # for more than the env allows simply gets the env's answer, and the feed
    # line below says so, because "why did this not auto-approve" must be
    # answerable without reading two configs and a deployment.
    project_ceiling = project_ceiling_of(policy)
    ceiling = global_ceiling
    if project_ceiling is not None and order.index(project_ceiling) < order.index(
        ceiling
    ):
        ceiling = project_ceiling
    if order.index(risk) > order.index(ceiling):
        return False

    # #1594: заморозка проекта - тот же допуск, что у ручного одобрения, и
    # автоодобрение ему подчиняется. Молча остаться черновиком было бы
    # ошибкой наблюдаемости: «почему не автоодобрено» отвечает строка ленты.
    refusal = freeze_admission(project, row["work_type"], row["freeze_rationale"] or "")
    if refusal is not None:
        await _note_freeze_refusal(db, task_id, refusal.text)
        return False

    transitioned = await repo.transition_status_if(
        db, task_id, expected_from="draft", new_status="open"
    )
    if not transitioned:
        return False

    from hub.services.dor import record_statement_paths, record_workspace_missing

    if dor_checks:
        await record_statement_paths(db, task_id, dor_checks)
    await record_workspace_missing(db, task_id)

    reasons = deserialize_str_list(row["risk_class_reasons"])
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        (
            f"Автоодобрено политикой проекта {project_slug} (dor=auto): класс "
            f"{risk.value} не выше действующего потолка {ceiling.value}. "
            f"Потолки: проектный "
            f"{project_ceiling.value if project_ceiling else '—'}, глобальный "
            f"{global_ceiling.value} (HAIPLANE_AUTO_APPROVE_MAX_CLASS={mode}). "
            f"Признаки: {'; '.join(reasons) or '—'}."
        ),
        author_kind="hub",
    )
    # actor=policy (#744): distinguishable from a human click AND from other
    # hub service writes — the human_gates metric (#737) already excludes it
    # by this name, so the autopilot shows up as its own line, not as noise
    # in either column.
    await repo.insert_event(
        db,
        kind="task_approved",
        task_id=task_id,
        actor="policy",
        payload={
            "auto": True,
            "risk_class": risk.value,
            "ceiling": ceiling.value,
            "project_ceiling": project_ceiling.value if project_ceiling else None,
            "global_ceiling": global_ceiling.value,
            "project": project_slug,
        },
    )
    log.info(
        "auto-approved draft #%s at class %s (project %s)",
        task_id,
        risk.value,
        project_slug,
    )
    return True
