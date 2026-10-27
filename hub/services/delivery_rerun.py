"""Один автоперезапуск упавших job канонического CI на свежей базе (#1956).

Гейт доставки слил базу в ветку PR, CI на новой голове красный. Раньше это был
терминальный ``fresh_base_ci_failed``: владелец сам выяснял, что упало, и писал
``decide rework``. Нестабильный тест лечится перезапуском, остальное - возвратом,
текст которого хаб может подготовить. При политике проекта
``delivery_fresh_base_rerun=on`` (по умолчанию off - поведение как до правки):

1. ``record_base_update`` в момент слияния базы пишет долговечное происхождение
   (``delivery_reruns``, state=armed). Признак «базу обновил гейт» и расход
   лимита больше не зависят от ленты событий (она чистится через 14 дней);
2. первый красный CI на этой голове (``gate_step``): допустимость (GitHub,
   проект default или спутник с ЯВНЫМ ``ci_contract``, текущая попытка workflow
   контракта завершена и неуспешна), бронь ОДНИМ условным UPDATE в транзакции,
   которая перечитывает живую задачу и политику, КОММИТ брони и только потом
   платный POST ``rerun-failed-jobs``. Второго POST в поколении нет никогда;
3. пока бронь открыта, ``gate_step`` отвечает на КАЖДОМ проходе доставки при
   любом исходе пробы PR (pending/passed/failed): мерж разрешён, только если
   доверенный отчёт попытки ``attempt_before+1`` этого же run verified, зелёный,
   и эта попытка ТЕКУЩАЯ по форжу при актуальном контракте. Иначе - ждём
   (``fresh_base_ci_rerun``, временная причина) до ``WAIT_MINUTES`` от брони;
4. любой другой исход - к человеку (``fresh_base_ci_failed``) с готовой
   инструкцией: упавшие job, фактическая база, образец текста ``decide rework``.
   Возврат и решение остаются за владельцем.

Неизвестное не равно «нет»: не прочиталась голова PR, список прогонов или
исход POST - это тоже к человеку, а не повод для автоматики.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_delivery_rerun as journal
from hub.db import write_transaction
from hub.integrations import git_ops as git_ops_mod
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.integrations.registry import plugins
from hub.services import ci_contract, ci_trust, project_policy

log = logging.getLogger("hub")

#: Временная причина: перезапуск идёт, мержа нет (TRANSIENT_GATE_PREFIXES).
RERUN_PREFIX = "fresh_base_ci_rerun"
#: Терминальная причина к человеку (тот же префикс, что был до правки).
FAILED_PREFIX = "fresh_base_ci_failed"
#: Сколько ждать новую попытку и её доверенный отчёт от брони.
WAIT_MINUTES = 45
WAIT_HINT = (
    "Это временное состояние, решение человека не требуется: хаб один раз "
    "перезапустил упавшие job и доставит задачу сам, когда придёт доверенный "
    "зелёный отчёт новой попытки. Пересдавать НЕ нужно и вредно: пересдача "
    "сбросит вердикт (#612)."
)
#: Итоги job и прогона, при которых перезапускать нечего.
_GREEN = ("success", "neutral", "skipped")
#: Исходы, с которыми задача ушла к человеку: повторный проход говорит то же.
HUMAN_OUTCOMES = (
    "rerun_red",
    "post_refused",
    "post_unsupported",
    "post_unknown",
    "read_failed",
    "timeout",
    "stalled_after_restart",
    "rerun_not_current",
)

IDENTITY_CHANGED = "проект или контракт CI сменились после выбора области"
#: Брони, чей POST идёт ВНУТРИ этого процесса. Бронь ``reserved`` вне этого
#: множества - POST оборван рестартом: ответ неизвестен, повторять нельзя.
_in_flight: set[int] = set()


def forget_in_flight() -> None:
    """Забыть идущие POST (рестарт; тесты)."""
    _in_flight.clear()


@dataclass(frozen=True)
class Scope:
    """Куда и каким workflow ходит перезапуск проекта задачи."""

    gh_repo: str
    workflow: str
    contract: Any
    repo: str | None
    gh_arg: str | None
    forge: str
    base: str
    identity: tuple[str, str, str, str] = ("", "", "", "")


async def _identity(
    db: aiosqlite.Connection, task_id: int
) -> tuple[str, str, str, str] | None:
    """Куда уйдёт запрос: репозиторий, форж, workflow и отпечаток контракта ЖИВОГО проекта."""
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return None
    contract = await ci_contract.contract_for_task(db, task_id)
    return (
        str(project["repo"] or ""),
        project_policy.forge_of(project),
        contract.workflow_path,
        contract.fingerprint,
    )


def base_text(task: dict[str, Any], ci: CIProbeResult) -> str:
    """Причина к человеку, как она читалась до правки (#1419)."""
    run = f" ({ci.details})" if ci.details else ""
    return (
        f"{FAILED_PREFIX}: CI красный на свежей базе — гейт слил базу в ветку PR "
        f"#{task['pr_number']}, и прогон на новой голове упал: {ci.reason}{run}. "
        "Авторская правка не менялась; это конфликт с уже доставленной работой, "
        "мержа нет"
    )


# ---- происхождение и классификация ------------------------------------------


async def policy_mode(db: aiosqlite.Connection, task_id: int) -> bool | None:
    """Включён ли перезапуск у проекта задачи СЕЙЧАС: True/False, None - не прочиталось.

    ``gate_policy_for_task`` на сбое чтения отвечает ``{}``, то есть «выключено»:
    для открытой брони это снимало бы защиту, приняв сбой за ``on -> off``.
    """
    try:
        project = await repo.resolve_project_for_task(db, task_id)
        if project is None:
            return False
        policy = json.loads(project["gate_policy"] or "{}")
    except Exception:  # noqa: BLE001 - «не прочиталось» не равно «выключено»
        log.exception("policy of task #%s unreadable", task_id)
        return None
    if not isinstance(policy, dict):
        return None  # битая политика - тоже «не прочиталось», а не «off»
    return (
        project_policy.delivery_fresh_base_rerun_of(policy)
        == project_policy.DELIVERY_FRESH_BASE_RERUN_ON
    )


async def policy_on(db: aiosqlite.Connection, task_id: int) -> bool:
    """Только подтверждённое ``on``: нечитаемая политика перезапуск не разрешает."""
    return await policy_mode(db, task_id) is True


async def record_base_update(
    db: aiosqlite.Connection, task: dict[str, Any], from_sha: str
) -> None:
    """Гейт слил базу в ветку: записать происхождение, пока политика on (#1956)."""
    if not await policy_on(db, int(task["id"])):
        return
    await journal.arm(
        db,
        int(task["id"]),
        int(task.get("submission_generation") or 0),
        pr_number=int(task["pr_number"]),
        base_from_sha=from_sha,
    )


async def is_fresh_base(
    db: aiosqlite.Connection, task: dict[str, Any], row: dict[str, Any] | None
) -> bool:
    """Обновлял ли гейт базу в этом поколении: долговечная запись, затем события."""
    if row is not None:
        return True
    from hub.services.orchestration import gate_branch_update_from

    return bool(await gate_branch_update_from(db, task))


async def fresh_base_origin(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    """Классификация «свежая база» для красного CI: запись поколения или события."""
    row = await journal.get(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    return await is_fresh_base(db, task, row)


# ---- вход из гейта ----------------------------------------------------------


async def gate_step(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
) -> str:
    """Что перезапуск говорит гейту на ЭТОМ проходе: "" - ничего, иначе причина.

    Зовётся из ``merge_before_completion`` после пробы PR и ДО её разбора, при
    любом исходе пробы: ожидающая бронь не должна зависеть от того, что PR
    прямо сейчас показывает. "" - мерж решают обычные правила.
    """
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    mode = await policy_mode(db, task_id)
    enabled = mode is True
    await _supersede_foreign(db, task)
    row = await journal.get(db, task_id, generation)
    if mode is False:
        # off: бронь ЛЮБОГО состояния на доставку не влияет; открытую снимаем.
        if row is not None and row["state"] in journal.OPEN_STATES:
            await _close(db, task, row, "superseded", "политика выключена")
        return ""
    if row is not None and row["state"] in journal.OPEN_STATES:
        if row["pr_number"] != int(task.get("pr_number") or 0):
            # Снимок вызывающего - старый PR того же поколения: чужую бронь он не
            # судит и не закрывает, мержа по ней в этом проходе нет.
            return _wait_text(task, "бронь относится к другому PR, проход устарел")
        return await _open_step(db, task, ctx, row, mode)
    if row is not None and row["state"] == journal.CLOSED:
        return await _closed_step(db, task, ctx, ci, row)
    if not enabled or ci.outcome != CIProbeOutcome.failed:
        return ""
    if not await is_fresh_base(db, task, row):
        return ""
    return await _first_failure(db, task, ctx, ci)


async def merge_hold(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> str:
    """Бронь в ЕДИНОЙ точке мержа (``merge_if_approval_stands``): "" - мерж не запрещён.

    Через эту точку идут все пути: ``merge_before_completion`` и headless-конвейер.
    Проверка стоит непосредственно перед вызовом форжа и перечитывает всё одним
    снимком (текущая попытка, отчёт, срок), а не верит прошлому проходу.
    """
    row = await journal.get(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    mode = await policy_mode(db, int(task["id"]))
    if row is None or row["state"] == journal.ARMED or mode is False:
        return ""
    if row["state"] == journal.CLOSED:
        seen = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
        return await _closed_step(db, task, ctx, seen, row)
    if row["pr_number"] != int(task.get("pr_number") or 0):
        return _wait_text(task, "бронь относится к другому PR, проход устарел")
    return await _open_step(db, task, ctx, row, mode)


async def headless_rerun(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any], pr_num: Any
) -> bool:
    """Headless: обслужить бронь ДО ветвления по пробе PR; True - проход закончен.

    Срок, терминальный исход и инструкция владельцу не зависят от того, что PR
    показывает (pending, failed, passed): ожидание ждёт следующего свипа,
    терминальный исход один раз уходит владельцу.
    """
    hold = await merge_hold(db, task, ctx)
    return bool(hold) and await headless_hold(db, task, "rerun_hold", hold, pr_num)


async def headless_hold(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    step_value: str,
    detail: str,
    pr_num: Any,
) -> bool:
    """Headless-конвейер: чем кончился ``merge_if_approval_stands``; True - проход всё.

    ``in_flight`` - соседний путь мержит: следующий цикл посмотрит снова.
    ``rerun_hold`` - бронь перезапуска не пускает мерж: ожидание ждёт следующего
    цикла, исход к человеку записывается один раз и уводит задачу из свипа.
    """
    if step_value == "in_flight":
        return True
    if step_value != "rerun_hold":
        return False
    if detail.startswith(FAILED_PREFIX):
        from hub.services import delivery_writes, orchestration

        async def hand_over() -> None:
            await repo.add_task_update(db, int(task["id"]), "hub", "alert", detail)
            await delivery_writes.hand_headless_to_human(
                db, task, pr_num, FAILED_PREFIX
            )

        await orchestration.record_delivery_outcome(db, task, hand_over)
    return True


async def _supersede_foreign(db: aiosqlite.Connection, task: dict[str, Any]) -> None:
    """Открытые брони чужого поколения или PR закрыть: мержа по ним нет.

    «Чужое» решает ЖИВАЯ задача в самом UPDATE, а не снимок вызывающего: запоздавший
    проход поколения N не должен снять бронь N+1, открытую соседним соединением.
    """
    async with write_transaction(db):
        for row in await journal.supersede_stale(db, int(task["id"])):
            await repo.add_task_update(
                db, int(task["id"]), "hub", "status",
                f"Перезапуск CI закрыт (superseded): поколение или PR сменились, run {row['run_id']}.",
            )  # fmt: skip


async def _closed_step(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
    row: dict[str, Any],
) -> str:
    """Лимит поколения потрачен; исход к человеку мерж по этому поколению закрывает.

    Любой исход пробы PR (в том числе внезапно зелёный) - тот же ответ: по
    красной новой попытке, чужой ручной попытке или истёкшему сроку мержа нет,
    решает владелец. Второго POST нет никогда.
    """
    if row["outcome"] not in HUMAN_OUTCOMES:
        return ""
    shown = ci
    if ci.outcome != CIProbeOutcome.failed:
        shown = CIProbeResult(
            CIProbeOutcome.failed, str(row["outcome"]), details=f"run {row['run_id']}"
        )
    return human_text(task, ctx, shown, row, _history_of(row))


# ---- допустимость и первый перезапуск ---------------------------------------


async def _scope(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> tuple[Scope | None, str]:
    """Допустим ли перезапуск для проекта: ``(область, "")`` либо ``(None, причина)``."""
    task_id = int(task["id"])
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return None, "проект задачи не определён"
    if project_policy.forge_of(project) != "github":
        return None, "форж проекта не GitHub, перезапуск не поддерживается"
    explicit = project_policy.CI_CONTRACT_KEY in project_policy.gate_policy_of(project)
    if not (explicit or str(project["slug"] or "") == ci_contract.HUB_SLUG):
        return None, "у спутника нет явно настроенного контракта CI"
    contract = await ci_contract.contract_for_task(db, task_id)
    if not contract.workflow_path or not contract.fingerprint:
        return None, "контракт CI проекта не прочитан"
    # Адрес запроса берётся из ТОГО ЖЕ чтения проекта, что и identity: ctx мог
    # устареть, а POST должен уйти туда, куда указывает сверяемая identity.
    gh_arg = str(project["repo"] or "").strip() or None
    return (
        Scope(
            gh_repo=str(gh_arg or config.REPO_NAME),
            workflow=contract.workflow_path,
            contract=contract,
            repo=str(project["workspace_path"] or "").strip() or None,
            gh_arg=gh_arg,
            forge=project_policy.forge_of(project) if gh_arg else "",
            base=git_ops_mod._resolve_base(ctx.get("base_branch")),
            identity=(
                str(project["repo"] or ""),
                project_policy.forge_of(project),
                contract.workflow_path,
                contract.fingerprint,
            ),
        ),
        "",
    )


async def _canonical_attempt(
    scope: Scope, task: dict[str, Any], head: str
) -> tuple[dict[str, Any] | None, str]:
    """Текущая попытка workflow контракта на голове: ``(попытка, "")`` или ``(None, почему)``."""
    try:
        runs = await plugins.git_ops.ci_runs_for_sha(
            head,
            repo=scope.repo,
            gh_repo=scope.gh_arg,
            forge=scope.forge,
            workflow_path=scope.workflow,
        )
    except Exception as exc:  # noqa: BLE001 - «не прочиталось» не равно «нет»
        return None, f"список прогонов не прочитан: {exc}"
    if runs is None:
        return None, "список прогонов не прочитан"
    cur = ci_trust.current_attempt(runs, head, scope.gh_repo, scope.contract)
    if cur is None:
        return None, f"прогона {scope.workflow} по голове {head[:12]} нет"
    return cur, ""


async def _pr_head(scope: Scope, task: dict[str, Any]) -> str:
    try:
        return str(
            await plugins.git_ops.pr_head_sha(
                int(task["pr_number"]),
                repo=scope.repo,
                gh_repo=scope.gh_arg,
                forge=scope.forge,
            )
            or ""
        ).strip()
    except Exception:  # noqa: BLE001 - пустая голова = «не прочитали»
        log.exception("PR head of #%s unreadable", task.get("id"))
        return ""


async def _first_failure(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
) -> str:
    """Красный CI на свежей базе, лимит не тронут: допустимость, бронь, POST."""
    scope, why = await _scope(db, task, ctx)
    if scope is None:
        return human_text(task, ctx, ci, None, f"не выполнен: {why}")
    head = await _pr_head(scope, task)
    if not head:
        return human_text(task, ctx, ci, None, "не выполнен: голова PR не прочитана")
    if head != _pass_head(task):
        # Этот проход проверял одобрение и CI на другой голове: платный запрос
        # для головы, которую он не видел, не делаем.
        return human_text(
            task, ctx, ci, None,
            f"не выполнен: голова PR {head[:12]} не та, что проверял проход "
            f"({_pass_head(task)[:12]})",
        )  # fmt: skip
    cur, why = await _canonical_attempt(scope, task, head)
    if cur is None:
        return human_text(task, ctx, ci, None, f"не выполнен: {why}")
    if cur["status"] != "completed":
        why = "канонический прогон ещё не завершён"
    elif str(cur["conclusion"]) in _GREEN:
        why = "канонический прогон зелёный: красный PR из-за другого workflow"
    else:
        return await _reserve_and_post(db, task, ctx, ci, scope, head, cur)
    return human_text(task, ctx, ci, None, f"не выполнен: {why}")


async def _reserve_and_post(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
    scope: Scope,
    head: str,
    cur: dict[str, Any],
) -> str:
    if db.in_transaction:
        # Бронь обязана быть закоммитлена ДО платного POST, а чужую транзакцию
        # коммитить нельзя (write_transaction её не владелец).
        return human_text(
            task, ctx, ci, None,
            "не выполнен: соединение в чужой транзакции, бронь не закоммитить",
        )  # fmt: skip
    attempt = int(cur["run_attempt"] or 0)
    run_id = str(cur["run_id"])
    note = f"бронь: run {run_id}, попытка {attempt} красная ({cur['conclusion']}), голова {head[:12]}"
    row_id, why = await _reserve(db, task, scope, head, run_id, attempt, note)
    if row_id is None and why == IDENTITY_CHANGED:
        return human_text(task, ctx, ci, None, f"не выполнен: {why}")
    if row_id is None:
        return _wait_text(task, why)
    _in_flight.add(row_id)
    try:
        try:
            result = await plugins.git_ops.rerun_failed_jobs(
                run_id, repo=scope.repo, gh_repo=scope.gh_arg, forge=scope.forge
            )
        except Exception as exc:  # noqa: BLE001 - исход POST неизвестен, повторять нельзя
            log.exception("rerun of run %s failed to answer", run_id)
            result = CIRunRequestResult(
                CIRunRequestOutcome.unavailable,
                "rerun_call_raised",
                details=str(exc)[:200],
            )
        # Флаг держится, пока ответ не записан: сосед не должен принять
        # принятый POST за оборванный рестартом.
        return await _after_post(
            db, task, ctx, ci, scope, row_id, run_id, attempt, result
        )
    finally:
        _in_flight.discard(row_id)


async def _reserve(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    scope: Scope,
    head: str,
    run_id: str,
    attempt: int,
    note: str,
) -> tuple[int | None, str]:
    """Занять POST поколения: ``(id, "")`` либо ``(None, почему нет)``. Коммит здесь."""

    task_id = int(task["id"])
    pr = int(task["pr_number"])
    generation = int(task.get("submission_generation") or 0)
    async with write_transaction(db):
        # Перечитываем ЖИВОЕ: между чтением снимка и резервом могли отозвать
        # одобрение, выключить политику, сменить голову, влить PR.
        live = await repo.get_task(db, task_id)
        if (
            live is None
            or int(live["submission_generation"] or 0) != generation
            or int(live["pr_number"] or 0) != pr
            or (live["submission_sha"] or "").strip() != _pass_head(task)
            or not await approved_now(db, task)
            or await repo.pipeline_merge_recorded(db, task_id, pr)
            or not await policy_on(db, task_id)
        ):
            return None, "условия брони изменились"
        if await _identity(db, task_id) != scope.identity:
            return None, IDENTITY_CHANGED
        base_from = await _base_from(db, task)
        row_id = await journal.reserve(
            db, task_id, generation, pr_number=pr, base_from_sha=base_from,
            repo=scope.gh_repo, workflow=scope.workflow, head_sha=head,
            run_id=run_id, attempt_before=attempt, text=note,
        )  # fmt: skip
        if row_id is None:
            return None, "бронь поколения уже занята или потрачена"
        await repo.add_task_update(
            db, task_id, "hub", "status",
            f"Перезапуск упавших job (#1956): {note}. Запрос уходит один раз.",
        )  # fmt: skip
    return row_id, ""


def _pass_head(task: dict[str, Any]) -> str:
    """Голова, на которой стоит ЭТОТ проход: как у точки мержа (``_checked_sha``)."""
    return (task.get("_checked_sha") or task.get("submission_sha") or "").strip()


async def approved_now(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    """Действует ли ИМЕННО APPROVED на этой сдаче (опция auto_review=False не в счёт).

    ``approval_stands`` пропускает и задачу без ревью вовсе; для платного POST
    нужен настоящий вердикт, на котором держится доставка.
    """
    from hub.services import orchestration

    live = await repo.get_task(db, int(task["id"]))
    return (
        live is not None
        and orchestration.review_approved_for_current_submission(dict(live))
        and await orchestration.approval_stands(db, task)
    )


async def _base_from(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    from hub.services.orchestration import gate_branch_update_from

    return await gate_branch_update_from(db, task)


async def _after_post(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
    scope: Scope,
    row_id: int,
    run_id: str,
    attempt: int,
    result: CIRunRequestResult,
) -> str:
    """Записать исход POST условно: закрытую бронь поздний ответ не трогает."""
    if result.outcome is CIRunRequestOutcome.requested:
        async with write_transaction(db):
            marked = await journal.mark_requested(
                db, row_id, "GitHub принял перезапуск"
            )
            if marked:
                await repo.add_task_update(
                    db, int(task["id"]), "hub", "status",
                    f"Перезапуск принят: run {run_id}, новая попытка {attempt + 1}.",
                )  # fmt: skip
        if not marked:  # бронь закрыл другой проход: отвечаем по его исходу
            return await _settled_answer(db, task, ctx)
        return _wait_text(
            task, "перезапуск принят, ждём новую попытку", run_id, attempt
        )
    outcome = {
        CIRunRequestOutcome.declined: "post_refused",
        CIRunRequestOutcome.unsupported: "post_unsupported",
    }.get(result.outcome, "post_unknown")
    detail = f"{result.reason}: {result.details or ''}".strip(": ")
    names = await _names(scope, run_id, attempt)
    row = await _close_by_id(
        db, task, row_id, outcome, f"форж не принял перезапуск ({detail})", names,
        only_reserved=True,
    )  # fmt: skip
    if row is None:
        return await _settled_answer(db, task, ctx)
    return human_text(task, ctx, ci, row, _history_of(row))


# ---- открытая бронь ---------------------------------------------------------


async def _open_step(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    row: dict[str, Any],
    mode: bool | None,
) -> str:
    """Бронь открыта: проверить её на каждом проходе, мерж только по её результату."""
    scope, why = await _scope(db, task, ctx)
    if mode is None:
        # Нечитаемая политика - не «выключили»: защиту не снимаем, зовём человека.
        return await _to_human(
            db, task, ctx, scope, row, "read_failed", "политика проекта не прочиталась"
        )
    if not mode or not await approved_now(db, task):
        return await _supersede(
            db, task, ctx, row, "политика выключена или одобрения нет"
        )
    if scope is None:
        return await _to_human(db, task, ctx, None, row, "read_failed", why)
    head = await _pr_head(scope, task)
    if not head:
        return await _to_human(
            db, task, ctx, scope, row, "read_failed", "голова PR не прочитана"
        )
    if head != row["head_sha"]:
        return await _supersede(db, task, ctx, row, "голова PR сменилась")
    cur, why = await _canonical_attempt(scope, task, head)
    if cur is None and why.startswith("список прогонов не прочитан"):
        return await _to_human(db, task, ctx, scope, row, "read_failed", why)
    if row["state"] == journal.RESERVED:
        return await _reserved_step(db, task, ctx, scope, row, cur)
    return await _requested_step(db, task, ctx, scope, row, cur, head)


async def _reserved_step(db, task, ctx, scope, row, cur) -> str:
    """Бронь без ответа POST: идёт ли он в этом процессе, или оборван рестартом."""
    if row["id"] in _in_flight or not _stalled(row):
        # POST идёт, либо бронь только что зарезервирована и флаг ещё не виден.
        return _wait_text(
            task, "POST перезапуска ещё идёт", row["run_id"], row["attempt_before"]
        )
    if (
        cur is not None
        and cur["run_id"] == row["run_id"]
        and int(cur["run_attempt"] or 0) > int(row["attempt_before"])
    ):
        async with write_transaction(db):  # POST дошёл: ответ потерян, а попытка есть
            moved = await journal.mark_requested(
                db, row["id"], "новая попытка видна на форже"
            )
        if not moved:
            return await _settled_answer(db, task, ctx)
        return _wait_text(
            task, "новая попытка видна на форже", row["run_id"], row["attempt_before"]
        )
    return await _to_human(
        db, task, ctx, scope, row, "stalled_after_restart",
        "после рестарта бронь без ответа POST, а новая попытка не появилась; "
        "повторять платный запрос нельзя", only_reserved=True,
    )  # fmt: skip


async def _requested_step(db, task, ctx, scope, row, cur, head) -> str:
    """POST принят: ждать ТЕКУЩУЮ попытку before+1 и её доверенный зелёный отчёт."""
    before = int(row["attempt_before"])
    expected = before + 1
    late = deadline_passed(row)
    if cur is None or cur["run_id"] != row["run_id"]:
        return await _wait_or_timeout(
            db, task, ctx, scope, row, late, "ждём прогон перезапуска"
        )
    got = int(cur["run_attempt"] or 0)
    if got > expected:
        return await _to_human(
            db, task, ctx, scope, row, "rerun_not_current",
            f"попытка {expected} уже не текущая: началась {got}; мерж по ней запрещён",
        )  # fmt: skip
    if got < expected or cur["status"] != "completed":
        return await _wait_or_timeout(
            db, task, ctx, scope, row, late, f"попытка {expected} идёт"
        )
    if str(cur["conclusion"]) not in _GREEN:
        return await _to_human(
            db, task, ctx, scope, row, "rerun_red",
            f"попытка {expected} закончилась как {cur['conclusion']}",
        )  # fmt: skip
    return await _verify_green(db, task, ctx, scope, row, head, cur, late)


async def _verify_green(db, task, ctx, scope, row, head, cur, late) -> str:
    """Зелёная текущая попытка: мерж только при verified-отчёте именно этой попытки.

    Один снимок: ключ попытки у форжа (``cur``), у проекции (``standing``), у
    самого прочитанного отчёта и ожидаемый ``before+1`` обязаны совпасть; зелёность
    и срок считаются по ЭТОМУ отчёту. Срок - по времени прихода отчёта, а не
    прохода: отчёт в срок мержится и поздним проходом, поздний - не мержится.
    """
    expected_key = ci_trust.attempt_key(
        row["run_id"], str(int(row["attempt_before"]) + 1)
    )
    standing = await ci_trust.refresh_quietly(db, int(task["id"]), head)
    gap = "доверенный отчёт новой попытки не пришёл"
    if standing is None or not standing.verified:
        if standing is not None:
            gap = f"отчёт не подтверждён: {standing.reason}"
        return await _wait_or_timeout(db, task, ctx, scope, row, late, gap)
    # Версию берём у самой проекции (``standing.version``), а не отдельным чтением
    # после refresh: иначе чужая версия соседа, начавшего проверку попытки 3,
    # приписалась бы старой проекции попытки 2.
    version = standing.version  # версия, при которой записана ЭТА проекция
    report = await ci_trust.trusted_report(db, int(task["id"]), head)
    keys = {
        standing.attempt_key,
        (dict(report).get("attempt_key") if report is not None else None),
        ci_trust.attempt_key(str(cur["run_id"]), str(cur["run_attempt"])),
        expected_key,
    }
    if report is None or len(keys) != 1:
        gap = f"отчёт и текущая попытка не совпадают с ожидаемой {expected_key}"
        return await _wait_or_timeout(db, task, ctx, scope, row, late, gap)
    live = await ci_contract.contract_for_task(db, int(task["id"]))
    problem = ci_trust.green_gap(dict(report), scope.contract)
    if {dict(report).get("contract_fp"), scope.contract.fingerprint} != {
        live.fingerprint
    }:
        problem = "контракт CI менялся, пока проверялся отчёт"
    if problem is not None or row["workflow"] != scope.workflow:
        gap = problem or "контракт CI изменился с момента брони"
        return await _wait_or_timeout(db, task, ctx, scope, row, late, gap)
    in_time = await journal.report_in_time(
        db, int(task["id"]), head, row["run_id"], str(int(row["attempt_before"]) + 1),
        row["requested_at"], WAIT_MINUTES,
    )  # fmt: skip
    if in_time is False:
        return await _to_human(
            db, task, ctx, scope, row, "timeout",
            f"доверенный отчёт новой попытки пришёл позже {WAIT_MINUTES} мин от брони",
        )  # fmt: skip
    # Бронь НЕ закрывается: разрешение живёт, пока держится проверка, и
    # повторяется на каждом проходе; закроет её запись доставки (триггер).
    if not await _confirm_in_transaction(
        db, task, row, head, expected_key, scope.contract.fingerprint, version
    ):
        # CI или контракт изменились после чтения (или бронь закрыли): мержа нет.
        return await _settled_answer(db, task, ctx)
    return ""


async def _confirm_in_transaction(
    db, task, row, head: str, expected_key: str, fingerprint: str, version: int | None
) -> bool:
    """Решение о мерже - ВНУТРИ транзакции, держащей write-lock (#1956, круг 3).

    Последовательные чтения до неё видят каждое своё мгновение; между последним
    из них и lock соседнее соединение могло зафиксировать отчёт попытки 3 или
    сменить контракт. Здесь, под lock, перечитывается всё, что живёт в базе:
    версия состояния CI коммита, проекция (ключ попытки, trust, отпечаток
    контракта), живой отпечаток контракта и сама бронь. Сеть (текущая попытка у
    форжа) под lock не читается: её перечитал ``merge_hold`` перед транзакцией.
    """
    task_id = int(task["id"])
    async with write_transaction(db):
        shown = await repo.get_ci_run_report(db, task_id, head)
        return (
            version is not None
            and await repo.ci_state_version(db, task_id, head) == version
            and shown is not None
            and dict(shown).get("attempt_key") == expected_key
            and dict(shown).get("trust") == ci_trust.VERIFIED
            and dict(shown).get("contract_fp") == fingerprint
            and await ci_contract.fingerprint_for_task(db, task_id) == fingerprint
            and await journal.confirm_requested(db, row["id"])
        )


async def _wait_or_timeout(db, task, ctx, scope, row, late: bool, why: str) -> str:
    if late:
        return await _to_human(
            db, task, ctx, scope, row, "timeout",
            f"за {WAIT_MINUTES} мин не получен доверенный зелёный отчёт новой попытки ({why})",
        )  # fmt: skip
    return _wait_text(
        task, why, row["run_id"], row["attempt_before"], row["requested_at"]
    )


#: Сколько бронь ``reserved`` без видимого флага POST считается «идущей»: окно
#: между коммитом резерва и регистрацией флага плюс таймаут самого POST.
STALL_GRACE_SECONDS = 180


def _stalled(row: dict[str, Any]) -> bool:
    """Бронь ``reserved`` старше окна POST: флага нет, значит POST оборван."""
    try:
        at = datetime.strptime(str(row["requested_at"]), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return True
    return (datetime.now(UTC) - at).total_seconds() > STALL_GRACE_SECONDS


def deadline_passed(row: dict[str, Any]) -> bool:
    try:
        at = datetime.strptime(str(row["requested_at"]), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return True  # срок не прочитан: ждать вечно нельзя
    return datetime.now(UTC) - at > timedelta(minutes=WAIT_MINUTES)


# ---- закрытие и тексты ------------------------------------------------------


async def _close(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    row: dict[str, Any],
    outcome: str,
    text: str,
    **kw: Any,
) -> bool:
    async with write_transaction(db):
        closed = await journal.close(db, row["id"], outcome, text, **kw)
        if closed:
            await repo.add_task_update(
                db,
                int(task["id"]),
                "hub",
                "status",
                f"Перезапуск CI закрыт ({outcome}): {text}.",
            )
    return closed


async def _supersede(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    row: dict[str, Any],
    text: str,
) -> str:
    """Бронь устарела: закрыть superseded; "" - обычные правила, мержа по ней нет."""
    if await _close(db, task, row, "superseded", text):
        return ""
    return await _settled_answer(db, task, ctx)


async def _settled_answer(
    db: aiosqlite.Connection, task: dict[str, Any], ctx: dict[str, Any]
) -> str:
    """Бронь закрыл другой проход раньше нас: ответить по ЕГО исходу, не по своему.

    Гонка двух проходов не должна превратить чужой терминальный исход (таймаут,
    красная попытка) в мерж: закрытая бронь отвечает одинаково всем.
    """
    row = await journal.get(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )
    if row is None or row["state"] != journal.CLOSED:
        return _wait_text(task, "бронь меняет другой проход")
    seen = CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    return await _closed_step(db, task, ctx, seen, row)


async def _close_by_id(
    db, task, row_id: int, outcome: str, text: str, names, **kw: Any
) -> dict[str, Any] | None:
    """Закрыть бронь по id и вернуть закрытую строку; None - её закрыл другой проход."""
    row = {"id": row_id}
    if not await _close(db, task, row, outcome, text, failed_jobs=names, **kw):
        return None
    return await journal.get(
        db, int(task["id"]), int(task.get("submission_generation") or 0)
    )


async def _to_human(
    db,
    task,
    ctx,
    scope: Scope | None,
    row: dict[str, Any],
    outcome: str,
    why: str,
    **kw: Any,
) -> str:
    """Закрыть бронь исходом к человеку и вернуть причину с готовой инструкцией."""
    # Имена - из попытки, которая УЖЕ шла: после принятого POST это before+1,
    # пока ответа POST нет (reserved) - исходная.
    accepted = row["state"] == journal.REQUESTED
    attempt = int(row["attempt_before"]) + (1 if accepted else 0)
    names = await _names(scope, row["run_id"], attempt) if scope else None
    ci = CIProbeResult(
        CIProbeOutcome.failed, "checks_failed", details=f"run {row['run_id']}"
    )
    closed = await _close_by_id(db, task, row["id"], outcome, why, names, **kw)
    if closed is None:
        return await _settled_answer(db, task, ctx)
    return human_text(task, ctx, ci, closed, _history_of(closed))


async def _names(scope: Scope | None, run_id: str, attempt: int) -> list[str] | None:
    """Имена упавших job попытки; None - «список не прочитан»."""
    if scope is None or not run_id:
        return None
    try:
        jobs = await plugins.git_ops.ci_attempt_jobs(
            run_id,
            str(attempt),
            repo=scope.repo,
            gh_repo=scope.gh_arg,
            forge=scope.forge,
        )
    except Exception:  # noqa: BLE001 - основная причина остаётся, список не прочитан
        log.exception("jobs of run %s attempt %s unreadable", run_id, attempt)
        return None
    if not jobs:
        # GitHub перечисляет ВСЕ job существующей попытки, в том числе зелёные:
        # пустой список - попытки нет, а не «упавших нет».
        return None
    if any(not str(job.get("conclusion") or "") for job in jobs):
        # Job ещё идёт (итога нет): список неготов, и «упавших нет» про него
        # сказать нельзя - это «не прочитан», а не пустой ответ.
        return None
    named = []
    for job in jobs:
        if str(job.get("conclusion") or "") in _GREEN:
            continue
        steps = ", ".join(job.get("failed_steps") or [])
        named.append(f"{job.get('name')}" + (f" [шаги: {steps}]" if steps else ""))
    return named


def _history_of(row: dict[str, Any]) -> str:
    try:
        entries = json.loads(row.get("history") or "[]")
    except ValueError:
        entries = []
    return "; ".join(str(e.get("text") or "") for e in entries if isinstance(e, dict))


def _named(row: dict[str, Any] | None) -> bool:
    """Есть ли в строке настоящие имена упавших job (а не «не прочитан» и «нет»)."""
    try:
        names = json.loads((row or {}).get("failed_jobs") or "[]")
    except ValueError:
        return False
    return bool(names)


def _names_of(row: dict[str, Any] | None) -> str:
    if row is None:
        return "список не определён (перезапуска не было)"
    try:
        names = json.loads(row.get("failed_jobs") or "[]")
    except ValueError:
        names = None
    if names is None:
        return "список не прочитан"
    return ", ".join(names) if names else "упавших job в списке нет"


def human_text(
    task: dict[str, Any],
    ctx: dict[str, Any],
    ci: CIProbeResult,
    row: dict[str, Any] | None,
    history: str,
) -> str:
    """К человеку: прежняя причина плюс история и готовый текст ``decide rework``."""
    base = git_ops_mod._resolve_base(ctx.get("base_branch"))
    names = _names_of(row)
    todo = names if _named(row) else "причину красного CI (см. прогон)"
    return (
        f"{base_text(task, ci)}. Перезапуск: {history or 'не выполнялся'}. "
        f"Упавшие job: {names}. Готовый текст возврата (решение и /decide за "
        f"владельцем): decide rework с комментарием «Влить свежую {base}, "
        f"починить: {todo}. Пересдать обычным submit-for-review; merge-only "
        "определит сверка диффа.»"
    )


def _wait_text(
    task: dict[str, Any],
    why: str,
    run_id: str = "",
    attempt_before: int = 0,
    since: str = "",
) -> str:
    where = f", run {run_id}, попытка {int(attempt_before) + 1}" if run_id else ""
    until = f", ждём не дольше {WAIT_MINUTES} мин от {since}" if since else ""
    return f"{RERUN_PREFIX}: {why} — PR #{task['pr_number']}{where}; мержа нет{until}"


# ---- карточка ---------------------------------------------------------------


async def card_view(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """История перезапуска для TaskView: None, пока перезапуска не было."""
    row = await journal.latest(db, task_id)
    if row is None or row["state"] == journal.ARMED:
        return None
    try:
        history = json.loads(row["history"] or "[]")
    except ValueError:
        history = []
    try:
        failed = json.loads(row["failed_jobs"] or "[]")
    except ValueError:
        failed = None
    return {
        "state": row["state"],
        "outcome": row["outcome"],
        "generation": row["submission_generation"],
        "pr_number": row["pr_number"],
        "workflow": row["workflow"],
        "head_sha": row["head_sha"],
        "run_id": row["run_id"],
        "attempt_before": row["attempt_before"],
        "requested_at": row["requested_at"],
        "closed_at": row["closed_at"],
        "failed_jobs": failed,
        "history": history,
    }
