"""Work that never left the branch, named when it is called done (#498).

The hub learned today to tell "merged" from "running in production" (#497).
This is the earlier loss: a task finished with commits on its branch and no
pull request at all — delivery never started, and the report read exactly like
a delivered one.

The tests are as much about the silences as about the warning: a task without a
branch, without a workspace, or with a git that will not answer must produce
nothing. An accusation made out of ignorance is worse than saying nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import MERGE_UNCONFIRMED
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskStatus
from hub.services import validation_run
from hub.services.delivery_gate import undelivered_warning
from hub.services.orchestration import (
    STACK_UNKNOWN_PREFIX,
    STACKED_BASE_PREFIX,
)
from hub.services.delivery_state import (
    DELIVERED,
    PR_CLOSED,
    PR_OPEN,
    UNKNOWN,
)
from tests.test_accept_without_delivery import (
    _alerts,
    _approved_task,
    _decide_deliver,
    _install,
    _MergeSpy,
)
from tests.test_pair_merge_gate import (
    _approved_pair_task,
    _drain_pair_delivery,
    _git,
    _git_with_state,
    _report_done,
)


async def _running_task(client: AsyncClient, title: str = "Undelivered?") -> int:
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    return task_id


def _workspace_with_changes(monkeypatch, changed: list[str] | None) -> None:
    """Point the check at a workspace whose branch reports ``changed``.

    ``None`` stands for "git would not answer" — the case that must stay quiet.
    """
    from hub import app as hub_app
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops, "branch_diff_paths", AsyncMock(return_value=changed)
    )


async def test_commits_without_pr_warn_on_done(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#498): changes on the branch, no PR, no merge — delivery never
    # started, and the report must say so instead of reading like any other.
    _workspace_with_changes(monkeypatch, ["hub/web.py", "tests/test_web.py"])
    task_id = await _running_task(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    assert resp.status_code == 200, resp.text
    warnings = resp.json()["warnings"]
    assert warnings, "undelivered work must be named on the report itself"
    assert "не начала доставляться" in warnings[0]
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] in ("completed", "review", "ci_check"), (
        "the warning is advisory — completion must not be blocked"
    )


async def test_delivered_work_is_not_warned_about(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#498): a PR (or a merge the hub performed) means delivery started.
    # Warning here would train people to ignore the warning.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)
    await repo.update_task(db, task_id, pr_number=4242)
    await db.commit()

    with_pr = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    await repo.update_task(db, task_id, pr_number=None)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 4243, task_id, "merged-somewhere"),
    )
    await db.commit()
    with_merge = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    assert with_pr == ""
    assert with_merge == ""


async def test_unknown_delivery_stays_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#498): three ways of not knowing, three silences. This is the same
    # line #839, #497 and #883 hold, turned around: there an absence could not
    # be printed as denial, here it cannot be printed as fault.
    task_id = await _running_task(client)
    task = dict(await repo.get_task(db, task_id))

    # git will not answer
    _workspace_with_changes(monkeypatch, None)
    assert await undelivered_warning(db, task) == ""

    # the branch changes nothing
    _workspace_with_changes(monkeypatch, [])
    assert await undelivered_warning(db, task) == ""

    # no branch at all: research and decisions were never meant to leave one
    await repo.update_task(db, task_id, branch=None)
    await db.commit()
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    assert await undelivered_warning(db, dict(await repo.get_task(db, task_id))) == ""


async def test_warning_reaches_the_task_feed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#498): the agent reads the response, the owner reads the feed.
    # A warning in only one of the two reaches nobody who can act on it — the
    # defect #826 found in review findings.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)

    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    updates = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    alerts = [u for u in updates if u["kind"] == "alert"]
    assert any("не начала доставляться" in u["content"] for u in alerts), (
        "the owner reads the feed, not the agent's response"
    )


async def test_missing_run_within_window_keeps_task_running(
    db: aiosqlite.Connection,
) -> None:
    # #1041 AC-1: workflows exist, this SHA has no run yet, the grace window
    # has not elapsed — delivery waits. needs_decision would turn a GitHub
    # registration lag into a human chore. The stand-in is not hex: detect-secrets
    # treats hex high-entropy strings as secrets and that scan is the same CI
    # job the delivery gate reads.
    g = _git(CIProbeOutcome.passed, merged=True)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details="head-sha-not-hex"
        )
    )
    plugins.git_ops = g
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running"
    assert task["ci_check_started_at"], (
        "the window is measured from this stamp; without it every later "
        "done report treats elapsed as 0 and the task waits forever"
    )
    g.merge_pr.assert_not_awaited()
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert not any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    )


async def test_missing_run_after_window_escalates_with_named_fact(
    db: aiosqlite.Connection,
) -> None:
    # #1041 AC-2: the same fact past the window is a decision, and the reason
    # names the fact and the commit — not the old ci_absent: no_workflow_runs
    # slug that hid "GitHub has not registered a run yet" as "there is no CI".
    g = _git(CIProbeOutcome.passed, merged=True)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details="head-sha-not-hex"
        )
    )
    plugins.git_ops = g
    task_id = await _approved_pair_task(db)
    await db.execute(
        "UPDATE tasks SET ci_check_started_at = datetime('now', '-30 minutes') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ci_absent: no_workflow_runs" not in body
    assert "workflow есть" in body
    assert "head-sha-not-hex" in body


# ---- #1186: mergeable is not deliverable when the base is still unmerged ----
#
# The incident, 06.09.2026 on spike-bo: task #1183's branch was cut from
# #1175's, #1183 got an auto verdict and a green CI, and the gate squash-merged
# its PR while #1175 was still in review. All five commits of the stack landed
# in main under #1183's number; #1175's PR went empty and DIRTY, its task stuck
# in review with a diff there was nowhere left to apply, and the attribution
# is gone for good. The signal existed the whole time — detect_branch_stacking
# — but only ever addressed a human. The gate asked about CI and mergeability
# and merged, and said so in the feed: "Условия доставки были выполнены
# целиком, ждать было нечего."


async def _base_task_in_review(db: aiosqlite.Connection, branch: str) -> int:
    """Another task whose branch is alive and unmerged — a stack's base."""
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title="Base of the stack"))
    await repo.update_task(db, tv.id, status="review", branch=branch)
    await db.commit()
    return tv.id


def _probes(g, outcome, reason: str = "scripted", details: str = ""):
    """Script the stacking probe on a git double (#1186).

    ``details`` matters for ``ref_unresolved``: the real probe puts the names
    it could not resolve there, and #1204 reads them to tell "that candidate's
    branch is gone" from "this machine could not answer at all".
    """
    from hub.integrations.protocols import StackProbeResult

    g.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(outcome=outcome, reason=reason, details=details)
    )
    return g


async def test_delivery_holds_while_the_base_branch_is_unmerged(
    db: aiosqlite.Connection,
) -> None:
    # AC-1: approved, green CI, mergeable — and the branch stands on the
    # unmerged branch of a task still in review. The merge is the irreversible
    # step, so it does not happen, and the feed names WHICH task is being
    # waited for. A hold, not an escalation: the base merges on its own and
    # this delivery becomes possible the moment it does.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    base_id = await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "an unmerged base is a wait, not a decision: needs_decision is a door "
        "that only opens outward (#1030)"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body, "the feed must name the task being waited for"
    assert "task-1175/image-capture" in body
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert not any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    )


async def test_delivery_proceeds_once_the_base_has_merged(
    db: aiosqlite.Connection,
) -> None:
    # AC-2: the same stack, after the base has been delivered. A completed
    # task owns no unmerged branch, so there is nothing left to compare
    # against and the hold lifts by itself — no second signal to maintain.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    task_id = await _approved_pair_task(db)
    base_id = await _base_task_in_review(db, "task-1175/image-capture")
    await repo.update_task(db, base_id, status="completed")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1


async def test_delivery_unaffected_without_a_stack(
    db: aiosqlite.Connection,
) -> None:
    # AC-3: another task's branch IS alive, and the probe looked and found
    # them independent. Delivery behaves exactly as it did before #1186 —
    # the new condition must not start holding ordinary deliveries.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.clear)
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-900/unrelated-work")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "стопк" not in body.lower(), (
        "a delivery that was checked and found independent says nothing new"
    )


async def test_unknown_stacking_is_not_read_as_no_stacking(
    db: aiosqlite.Connection,
) -> None:
    # AC-4. The trap this whole task is about, and the one the statement
    # named one layer too shallow: the predicate returned a bool, so a git
    # that could not answer — refs missing from the clone, rev-list failing,
    # no workspace — came back with the very same False that means "checked,
    # and they are independent". Advisory, that cost a missing hint. As a
    # delivery condition it costs the base task its work.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
    )
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "a call that did not land is cured by asking again, not by a human"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ref_unresolved" in body, "the reason it could not look is named"


async def test_bool_only_plugin_is_unknown_rather_than_clear(
    db: aiosqlite.Connection,
) -> None:
    # AC-4, the other half: a plugin that predates the probe answers only
    # True/False, and its False cannot distinguish the two. Delivery goes
    # ahead — refusing would stall every delivery on such a plugin forever,
    # which is the constraint's other side — but it is said out loud, so a
    # reader can tell "we could not look" from "we looked and there was
    # nothing". Today the two are the same silence.
    g = _git(CIProbeOutcome.passed, merged=True)
    # A pre-#1186 plugin, declared the way this repo already declares one
    # (tests/test_stack_advisory.py does the same to branch_ancestry): the
    # attribute is simply not there to be found.
    g.branch_stacking_probe = None
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "an unanswerable plugin must not stall"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "подтвердить не удалось" in body
    assert "legacy_bool_predicate" in body


# ---- #1186, второй раунд: находки машинного ревью этой же правки ----


async def _base_task_with_status(
    db: aiosqlite.Connection, branch: str, status: str
) -> int:
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title=f"Base in {status}"))
    await repo.update_task(db, tv.id, status=status, branch=branch)
    await db.commit()
    return tv.id


async def test_an_escalated_base_is_still_a_base(db: aiosqlite.Connection) -> None:
    """Основание уходит из running/review не только через доставку.

    Первая версия этой правки искала основание среди running/review на
    посылке «оттуда выходят только доставившись». Посылка неверна: гейт
    самой базовой задачи эскалирует её в needs_decision по красному CI, по
    исчерпанному бюджету починки, по отказу мержа — и ветка при этом остаётся
    ровно такой же несмерженной. Инцидент 06.09 воспроизводился бы через эту
    дверь целиком, просто с другим триггером.
    """
    from hub.integrations.protocols import StackProbeOutcome

    for status in ("needs_decision", "fix_requested", "ci_check"):
        g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
        g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
        task_id = await _approved_pair_task(db)
        base_id = await _base_task_with_status(db, "task-1175/image-capture", status)

        await _report_done(db, task_id)

        g.merge_pr.assert_not_awaited()
        updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
        body = " ".join(u.get("content") or "" for u in updates)
        assert f"#{base_id}" in body, (
            f"основание в статусе {status} владеет несмерженной веткой "
            f"ровно так же, как в review"
        )
        # Both rows out of the candidate set before the next round. The walk
        # answers with the FIRST stacked pair it finds, and the task just held
        # is itself a running branch — leaving either behind would have the
        # next round name a row from this one and test nothing new.
        await repo.update_task(db, base_id, status="completed", branch="")
        await repo.update_task(db, task_id, status="completed", branch="")
        await db.commit()


async def test_the_same_commit_under_two_names_never_waits_on_itself(
    db: aiosqlite.Connection,
) -> None:
    """Взаимное удержание — это тишина навсегда, а не осторожность.

    Когда две ветки указывают на один коммит, предикат истинен в ОБЕ
    стороны: каждая видит другую своим основанием. Удержание транзитное, то
    есть молчаливое по построению, — значит обе задачи встали бы навсегда и
    никто бы об этом не узнал. Ждать здесь нечего и некого, и это вопрос к
    человеку, а не к следующему циклу поллера.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="same_tip")
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited(), "отказ от догадки — не разрешение мержить"
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "ожидание с обеих сторон не разрешается ничем: тут нужен человек"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ждать нечего и некого" in body
    assert "SAME commit" in body, "объяснение формы стопки берётся из #1193"


async def test_a_held_delivery_is_not_told_to_wait_for_a_green_ci(
    db: aiosqlite.Connection,
) -> None:
    """Попасть в кортеж транзитных отказов — только половина вступления в него.

    У этого потребителя есть лесенка формулировок под каждую причину, и
    непрописанный в ней член наследует фразу про CI. Здесь она ложна дважды:
    CI уже зелёный, а совет «отчитайтесь о готовности снова» — ровно то
    действие, которое сбросило бы вердикт (#612).
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1175/image-capture")

    await _report_done(db, task_id)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "когда CI станет зелёным" not in body, (
        "CI уже зелёный — ждут доставки другой задачи, а не проверки"
    )
    assert "Пересдавать НЕ нужно" in body


async def test_the_base_of_a_stack_merges_first_instead_of_asking(
    db: aiosqlite.Connection,
) -> None:
    """Нижняя ветка стопки доставляется, а не эскалируется.

    Предикат стопки симметричен: основание видит собственного потомка как
    «стопку». Первая версия этого гейта свалила head_is_ancestor в одну кучу
    с формами, у которых порядок не выводится, и отправляла к человеку
    ОСНОВАНИЕ каждой сознательной стопки — при том что собственная подсказка
    хаба в этот же момент говорит обратное: «'{branch}' merges into '{base}'
    FIRST» (#1184).

    Проверено на настоящем репозитории, а не выведено: для нижней ветки
    rev-list даёт total=2, excluded=1, то есть stacked=True, ancestry даёт
    head_is_ancestor, а git diff develop..нижняя показывает ровно её
    собственный файл. Мерж безопасен, ждать нечего, решать нечего.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_ancestor")
    task_id = await _approved_pair_task(db)
    await _base_task_in_review(db, "task-1204/built-on-top-of-me")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "у основания стопки нет ни того, кого ждать, ни того, что решать"
    )
    assert g.merge_pr.await_count == 1


async def test_a_branch_from_another_project_never_holds_a_delivery(
    db: aiosqlite.Connection,
) -> None:
    """Чужой проект — чужой репозиторий, и его имена веток здесь ничего не значат.

    Самая тяжёлая находка ревью этой правки. Перечень кандидатов не
    фильтровался по проекту, а на проде их девять, у каждого свой клон.
    Ветка задачи из другого проекта в ЭТОМ репозитории не разрешается, проба
    отвечает unavailable, а он по правилу #1186 старше clear — и держит
    доставку. Строка никогда не разрешится, значит удержание вечное; одной
    такой строки хватает, и держит она доставки ВО ВСЕХ проектах сразу.

    Проверяется по исходу, а не по числу вызовов: доставка обязана пройти.
    """
    from hub.integrations.protocols import StackProbeOutcome, StackProbeResult
    from hub.models import TaskCreate
    from hub.services import create_task

    g = _git(CIProbeOutcome.passed, merged=True)
    # Проба честно не может разрешить чужую ссылку — ровно как настоящий git.
    g.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(
            outcome=StackProbeOutcome.unavailable,
            reason="ref_unresolved",
            details="task-42/in-another-repo",
        )
    )
    plugins.git_ops = g

    other_project_id = await repo.create_project(
        db,
        slug="other-repo",
        name="Other",
        repo_name="acme/other",
        workspace_path="/srv/other",
    )
    epic = await create_task(db, TaskCreate(title="Epic in another project"))
    await repo.update_task(db, epic.id, project_id=other_project_id)
    foreign = await create_task(db, TaskCreate(title="Work in another project"))
    await repo.update_task(
        db,
        foreign.id,
        status="review",
        branch="task-42/in-another-repo",
        parent_id=epic.id,
    )
    await db.commit()

    task_id = await _approved_pair_task(db)
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "ветка из чужого репозитория не может держать эту доставку"
    )
    assert g.merge_pr.await_count == 1


async def test_being_someones_base_does_not_excuse_standing_on_someone_else(
    db: aiosqlite.Connection,
) -> None:
    """Первое совпадение перестало быть безопасным, когда одно из них стало мержем.

    Пока любое совпадение вело к отказу, порядок обхода ничего не решал. Ветка
    «мы и есть основание» впервые сделала так, что одно совпадение может
    закончиться НЕОБРАТИМЫМ мержем — и тогда порядок решает всё.

    Три задачи: под нами настоящее несмерженное основание, а поверх нас стоит
    ещё одна. Обе связи истинны одновременно. Строки идут по возрастанию id, а
    id не повторяет топологию git — ветку можно перебазировать позже, задачу
    пересоздать. Если безопасное совпадение окажется первым, а опасное вторым,
    прежний код объявил бы нас основанием и влил бы чужую работу под нашим
    номером: ровно тот инцидент, ради которого условие и заведено, только
    вошедший через собственную починку.
    """
    from hub.integrations.protocols import StackProbeOutcome, StackProbeResult
    from hub.models import TaskCreate
    from hub.services import create_task

    g = _git(CIProbeOutcome.passed, merged=True)
    g.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(
            outcome=StackProbeOutcome.stacked, reason="shares_unmerged_commits"
        )
    )

    async def ancestry(branch, other_branch, repo=None):
        # Поверх нас — безопасно; под нами — опасно.
        return "head_is_ancestor" if "on-top" in other_branch else "head_is_descendant"

    g.branch_ancestry = AsyncMock(side_effect=ancestry)
    plugins.git_ops = g

    # Безопасная строка получает МЕНЬШИЙ id, то есть обходится первой.
    on_top = await create_task(db, TaskCreate(title="Built on top of us"))
    await repo.update_task(db, on_top.id, status="review", branch="task-C/on-top")
    real_base = await create_task(db, TaskCreate(title="Our actual base"))
    await repo.update_task(db, real_base.id, status="review", branch="task-D/under-us")
    await db.commit()
    assert on_top.id < real_base.id, "порядок обхода задан именно так"

    task_id = await _approved_pair_task(db)
    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "ждём доставки настоящего основания"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{real_base.id}" in body, "названо то основание, что под нами"


async def test_every_status_in_the_delivery_list_is_actually_seen(
    db: aiosqlite.Connection,
) -> None:
    """Перечень статусов проверяется целиком, а не одним членом.

    Мутационная проверка показала дыру: удаление pending_report из перечня не
    роняло ни одного теста. То есть список рос от правки к правке, а держало
    его только моё внимание — ровно то, что правило «правишь член множества —
    проверь всё множество» и запрещает. Тест перебирает КАЖДЫЙ член, поэтому
    удаление любого из них теперь видно.

    Сам pending_report найден облачным ревьюером: та же дверь, что
    needs_decision, и упущена по той же причине — список писался из тех
    статусов, что были в голове, а не из перечисления.
    """
    from hub.integrations.protocols import StackProbeOutcome
    from hub.services.orchestration import STACK_DELIVERY_STATUSES

    # Перечень выписан ЗДЕСЬ, а не взят из проверяемого кода. Первая версия
    # этого теста перебирала сам STACK_DELIVERY_STATUSES — и удаление члена
    # меняло разом и код, и тест, поэтому мутация его не роняла. Тест,
    # сверяющий код с самим собой, зелен по построению и не держит ничего.
    expected = (
        "running",
        "review",
        "ci_check",
        "fix_requested",
        "needs_decision",
        "pending_report",
    )
    assert set(STACK_DELIVERY_STATUSES) == set(expected), (
        "член добавлен или убран — решение осознанное, значит и здесь его надо "
        "назвать, а не унаследовать молча"
    )

    for status in expected:
        g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
        g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
        task_id = await _approved_pair_task(db)
        base_id = await _base_task_in_review(db, f"task-base/{status}")
        await repo.update_task(db, base_id, status=status)
        await db.commit()

        await _report_done(db, task_id)

        g.merge_pr.assert_not_awaited()
        updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
        body = " ".join(u.get("content") or "" for u in updates)
        assert f"#{base_id}" in body, (
            f"основание в статусе {status} владеет несмерженной веткой, "
            f"и условие обязано его видеть"
        )
        # Обе строки — из перечня кандидатов долой, иначе следующий круг
        # ответит основанием предыдущего и проверит тот же статус заново.
        await repo.update_task(db, base_id, status="completed", branch="")
        await repo.update_task(db, task_id, status="completed", branch="")
        await db.commit()


# ---------------------------------------------------------------------------
# #1233: мерж базы не должен стоить человеку второго вердикта, а расхождение
# с базой обязано всплывать ДО одобрения.
#
# ИЗМЕРЕННЫЙ СЛУЧАЙ, 09.09.2026. #1204 одобрена в 05:48, через 14 секунд —
# merge_failed: конфликт с только что доставленной #1205 в трёх файлах. Задача
# ушла в needs_decision, исполнитель слил базу и пересдался шестым разом, и тот
# же человек одобрял ту же работу второй раз. В тот же день #1206, #1208 и
# #1216 дописывали тесты в конец одного файла: каждая пара давала конфликт
# «оставить оба», безобидный по смыслу и стоивший круга ревью.


def _seeing(monkeypatch, tip: str, **kw):
    """Git-двойник с наблюдаемой вершиной ветки и клоном у проекта.

    Без клона resolve_branch_tip отвечает «неоткуда смотреть» раньше, чем
    спросит git, и любой сценарий про сдвиг вершины проверял бы пустоту.
    """
    from hub.services import orchestration

    g = _git(**kw)
    g.fetch_base = AsyncMock(return_value=(True, ""))
    g.head_sha = AsyncMock(return_value=tip)
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    return g


async def _feed(db, task_id) -> str:
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    return " ".join(u.get("content") or "" for u in updates)


_TAIL_CONFLICT = (
    "def test_one():\n"
    "    assert True\n"
    "<<<<<<< HEAD\n"
    "def test_from_the_branch():\n"
    "    assert True\n"
    "||||||| merged common ancestors\n"
    "=======\n"
    "def test_from_the_base():\n"
    "    assert True\n"
    ">>>>>>> origin/develop\n"
)

_OVERLAPPING_CONFLICT = (
    "def deliver():\n"
    "<<<<<<< HEAD\n"
    "    return remote_unreachable()\n"
    "||||||| merged common ancestors\n"
    "    return unknown()\n"
    "=======\n"
    "    return ci_absent()\n"
    ">>>>>>> origin/develop\n"
)


# ---- AC-1: расхождение названо ДО вердикта ----


async def test_a_future_conflict_is_named_before_the_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1 (#1233): ветка отстала от базы так, что мерж не будет чистым. Человек
    # обязан прочитать это в брифе ДО вердикта, с именами файлов, а не через
    # четырнадцать секунд ПОСЛЕ него — из отказа доставки, как было с #1204.
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services import review_brief

    g = _git()
    g.check_pr_mergeable = AsyncMock(
        return_value=(
            MergeabilityOutcome.conflicting,
            "конфликт с базовой веткой: hub/poller.py, tests/test_poller.py",
        )
    )
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        raising=False,
    )
    task_id = await _approved_pair_task(db, pr_number=1204)
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    brief = await review_brief.build_review_brief(db, task_id)

    assert brief is not None
    assert brief.base_merge.state == "conflicting", (
        "расхождение с базой обязано быть в брифе, а не всплывать отказом доставки"
    )
    assert brief.base_merge.files == ["hub/poller.py", "tests/test_poller.py"], (
        "имена конфликтующих файлов — это и есть то, что человек не мог узнать"
    )
    assert g.check_pr_mergeable.await_count == 1, (
        "расхождение считается там же, где его читает доставка, а не вторым счётом"
    )


async def test_a_clean_branch_does_not_cry_conflict_before_the_verdict(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Обратная сторона AC-1: тревога, которая звучит всегда, не значит ничего.
    # И «спросить не удалось» — это не «чисто»: исходы не схлопываются (#725).
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services import review_brief

    g = _git()
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        raising=False,
    )
    task_id = await _approved_pair_task(db, pr_number=1205)
    await repo.update_task(db, task_id, status="review")
    await db.commit()

    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.mergeable, "мерж пройдёт")
    )
    clean = await review_brief.build_review_brief(db, task_id)
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.unavailable, "gh не ответил")
    )
    blind = await review_brief.build_review_brief(db, task_id)

    assert clean is not None and blind is not None
    assert clean.base_merge.state == "clean"
    assert clean.base_merge.files == []
    assert blind.base_merge.state == "unknown", (
        "«спросить не удалось» обязано читаться как оно есть, а не как «чисто»"
    )


# ---- AC-2: мерж базы не убивает одобрение ----


async def test_merging_the_base_alone_keeps_the_verdict(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2 (#1233): в ветку приехала база и больше ничего. Вершина другая,
    # авторская работа та же — дифф ветки к базе совпал байт в байт. Вердикт
    # остаётся текущим, доставка идёт, второго одобрения человек не тратит.
    g = _seeing(monkeypatch, "approved0commit", merged=True)
    task_id = await _approved_pair_task(db)
    assert dict(await repo.get_task(db, task_id))["submission_sha"] == (
        "approved0commit"
    ), "предусловие: хаб закрепил одобренный коммит"

    g.head_sha = AsyncMock(return_value="tip0after0base0merge")
    g.branch_diff = AsyncMock(return_value="@@ -1,0 +2 @@\n+авторская строка\n")
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "мерж базы — это не новая работа, и платить за него вторым вердиктом "
        "человеку не за что"
    )
    assert g.merge_pr.await_count == 1
    assert g.branch_diff.await_count == 2, (
        "сравниваются ИМЕННО два диффа — одобренной вершины и текущей"
    )
    seen = [c.args[2] for c in g.branch_diff.await_args_list]
    assert seen == ["approved0commit", "tip0after0base0merge"]
    feed = await _feed(db, task_id)
    assert "Вердикт остаётся текущим" in feed, (
        "сохранённый вердикт без сказанного вслух основания — это доверие"
    )


async def test_an_author_edit_with_the_merge_drops_the_verdict(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # МУТАЦИОННЫЙ НАПАРНИК AC-2: правило «считать вердикт текущим при ЛЮБОМ
    # мерже» обязано ронять именно этот тест. Дифф к базе изменился — значит в
    # ветке есть байт, которого ревью не видело, и вердикт слетает, как сегодня.
    g = _seeing(monkeypatch, "approved0commit", merged=True)
    task_id = await _approved_pair_task(db)

    g.head_sha = AsyncMock(return_value="tip0with0author0edit")
    g.branch_diff = AsyncMock(
        side_effect=[
            "@@ -1,0 +2 @@\n+авторская строка\n",
            "@@ -1,0 +2,2 @@\n+авторская строка\n+и ещё одна, которой ревью не видело\n",
        ]
    )
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] != "completed"
    g.merge_pr.assert_not_awaited()
    feed = await _feed(db, task_id)
    assert "stale_approval" in feed
    assert "авторская правка" in feed, "отказ обязан назвать, ЧТО именно не сошлось"


async def test_an_unreadable_diff_never_keeps_the_verdict(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Третий исход, который нельзя схлопывать с двумя: git не ответил. Прочесть
    # молчание как «приехала только база» значило бы продлить вердикт на код,
    # которого никто не видел, — ровно то, ради чего #572 закреплял коммит.
    g = _seeing(monkeypatch, "approved0commit", merged=True)
    task_id = await _approved_pair_task(db)

    g.head_sha = AsyncMock(return_value="tip0after0base0merge")
    g.branch_diff = AsyncMock(return_value=None)
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] != "completed"
    g.merge_pr.assert_not_awaited()
    assert "прочитать не удалось" in await _feed(db, task_id)


# ---- AC-3 / AC-4: автомерж только названного класса ----


async def test_tail_only_additions_are_merged_and_named(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3 (#1233): конфликт из двух непересекающихся добавлений в конец файла —
    # образец 09.09 (#1206, #1208, #1216). Гейт разрешает его сам, «оставить
    # оба», гонит валидацию и судит её ПО КОДУ ВОЗВРАТА, а что именно сложено —
    # пишет в карточку. Человека здесь не будят.
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(
        return_value=({"tests/test_review_dispatch.py": _TAIL_CONFLICT}, "")
    )
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "merged0by0the0gate"))
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, validation_commands='["uv run pytest -q"]')
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] not in ("needs_decision", "completed"), (
        "решать человеку нечего: конфликт разрешён, доставка повторится циклом"
    )
    g.push_resolved_base_merge.assert_awaited_once()
    resolved = g.push_resolved_base_merge.await_args.args[4]
    merged_text = resolved["tests/test_review_dispatch.py"]
    assert "<<<<<<<" not in merged_text and ">>>>>>>" not in merged_text
    assert "test_from_the_branch" in merged_text and "test_from_the_base" in merged_text
    assert merged_text.index("test_from_the_branch") < merged_text.index(
        "test_from_the_base"
    ), (
        "наше идёт первым: этот порядок сохраняет смещение авторского блока, "
        "а с ним и вердикт, который AC-2 бережёт"
    )
    validate = g.push_resolved_base_merge.await_args.args[5]
    assert validate is not None, "автомерж без прогона валидации не бывает"
    feed = await _feed(db, task_id)
    assert "Автомерж базы" in feed and "хвостовые добавления" in feed, (
        "что именно сложено, обязано быть видно в карточке"
    )
    assert task["submission_sha"] == "merged0by0the0gate", (
        "коммит сдачи перезакреплён на коммит, который сделал сам гейт: иначе "
        "следующий круг увидит сдвинутую вершину и снимет вердикт, который "
        "автомерж только что спас — то есть автомерж отменит сам себя"
    )
    assert "перезакреплён" in feed, "перезакрепление коммита не бывает молчаливым"


async def test_an_overlapping_conflict_still_calls_a_human(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-4 (#1233): обе стороны правят одни и те же строки — случай #1204, он
    # смысловой. Автомержа НЕТ, задача идёт к человеку, и причина названа, а не
    # оставлена в виде «GitHub отказал в мерже».
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(
        return_value=({"hub/services/orchestration.py": _OVERLAPPING_CONFLICT}, "")
    )
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "should0not0happen"))
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, validation_commands='["uv run pytest -q"]')
    await db.commit()

    await _report_done(db, task_id)

    g.push_resolved_base_merge.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "смысловой конфликт обязан оставаться человеческим"
    )
    feed = await _feed(db, task_id)
    assert "одни и те же строки" in feed, (
        "человек читает причину, а не «GitHub отказал»"
    )


async def test_the_gate_never_merges_what_it_cannot_validate(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Ограничение из постановки: сложить два блока текста мало — они могут
    # конфликтовать по именам. Прогонять нечем — не пушим и зовём человека.
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(
        return_value=({"tests/test_review_dispatch.py": _TAIL_CONFLICT}, "")
    )
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "should0not0happen"))
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    g.push_resolved_base_merge.assert_not_awaited()
    assert "validation_commands" in await _feed(db, task_id)


async def test_a_merge_that_failed_without_a_conflict_is_not_dressed_as_one(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Отказ мержа бывает и не про конфликт (протухший токен, защита ветки).
    # Приписать ему конфликт значит поставить человеку неверный диагноз, а
    # «спросить не удалось» — это не «конфликта нет» (#725).
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(return_value=({}, ""))
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "should0not0happen"))
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    g.push_resolved_base_merge.assert_not_awaited()
    feed = await _feed(db, task_id)
    assert "merge_failed" in feed
    assert "Автомерж не применён" not in feed


async def test_a_probe_that_could_not_look_is_not_read_as_no_conflict(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Третий исход и здесь: проба мержа не состоялась. Прочесть это как
    # «конфликта нет» значит поставить человеку диагноз «GitHub отказал» там,
    # где гейт просто не смог посмотреть, — #725 с другой стороны. Найдено
    # мутацией: без этого теста подмена None на пустой словарь проходила молча.
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(return_value=(None, "клон не отвечает"))
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "should0not0happen"))
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    g.push_resolved_base_merge.assert_not_awaited()
    feed = await _feed(db, task_id)
    assert "проба мержа базы не удалась" in feed and "клон не отвечает" in feed, (
        "«посмотреть не удалось» обязано звучать как оно есть"
    )


# ---- класс автомержа, поштучно ----


def test_the_automerge_class_is_read_from_the_diff3_ancestor() -> None:
    # Признак берётся у git, а не у глаза: пустая секция общего предка = ни одна
    # сторона не переписала существующую строку. Непустая = переписала, и это
    # человеческий случай, как #1204.
    from hub.services import base_merge

    assert base_merge.classify_conflict(_TAIL_CONFLICT)[0] == base_merge.TAIL_ADDITIONS
    kind, why = base_merge.classify_conflict(_OVERLAPPING_CONFLICT)
    assert kind == base_merge.UNRESOLVABLE
    assert "одни и те же строки" in why

    not_at_the_end = _TAIL_CONFLICT + "def test_after_the_conflict():\n    pass\n"
    assert base_merge.classify_conflict(not_at_the_end)[0] == base_merge.UNRESOLVABLE
    assert base_merge.resolve_tail_additions(not_at_the_end) is None

    no_ancestor = _TAIL_CONFLICT.replace("||||||| merged common ancestors\n", "")
    assert base_merge.classify_conflict(no_ancestor)[0] == base_merge.UNRESOLVABLE, (
        "без разметки diff3 доказать «ничего не переписано» нечем"
    )


def test_one_file_outside_the_class_stops_the_whole_resolution() -> None:
    # Полуразрешённый мерж хуже неразрешённого: он выглядит как решение.
    from hub.services import base_merge

    resolutions, why = base_merge.plan_resolution(
        {"tests/a.py": _TAIL_CONFLICT, "hub/b.py": _OVERLAPPING_CONFLICT}
    )
    assert resolutions == {}
    assert "hub/b.py" in why


# ---- механика автомержа на НАСТОЯЩЕМ git, а не на двойнике ----


def _run_git(*args: str, cwd) -> None:
    import subprocess

    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _repo_with_a_tail_conflict(tmp_path):
    """Клон, где база и ветка дописали каждая свой хвост одного файла.

    Ровно случай 09.09.2026: #1206, #1208 и #1216 дописывали тесты в конец
    tests/test_review_dispatch.py, и каждая пара давала конфликт «оставить оба».
    """
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _run_git("git", "config", "user.email", "t@example.com", cwd=repo)
    _run_git("git", "config", "user.name", "t", cwd=repo)
    suite = repo / "tests_suite.py"
    suite.write_text("def test_common():\n    assert True\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-q", "-m", "common", cwd=repo)
    _run_git("git", "push", "-q", "origin", "develop", cwd=repo)

    _run_git("git", "checkout", "-q", "-b", "task-1233/probe", cwd=repo)
    suite.write_text(suite.read_text() + "\n\ndef test_from_the_branch():\n    pass\n")
    _run_git("git", "commit", "-q", "-am", "branch tail", cwd=repo)
    _run_git("git", "push", "-q", "origin", "task-1233/probe", cwd=repo)

    _run_git("git", "checkout", "-q", "develop", cwd=repo)
    suite.write_text(
        "def test_common():\n    assert True\n\n\ndef test_from_the_base():\n    pass\n"
    )
    _run_git("git", "commit", "-q", "-am", "base tail", cwd=repo)
    _run_git("git", "push", "-q", "origin", "develop", cwd=repo)
    return repo


async def test_the_automerge_is_judged_by_the_return_code_not_the_output(
    tmp_path,
) -> None:
    # Ограничение из постановки, и оно наблюдалось дважды 09.09: «All checks
    # passed» при коде возврата 2. Здесь настоящий git и настоящий конфликт:
    # красная валидация не пушит НИЧЕГО, зелёная — пушит слитую ветку, и в ней
    # лежат оба блока, авторский первым.
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")

    files, why = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, pinned
    )
    assert files, f"настоящий конфликт обязан быть виден: {why}"
    resolutions, note = base_merge.plan_resolution(files)
    assert resolutions and "хвостовые добавления" in note

    before = _tip(repo, "task-1233/probe")

    async def _red(_path):
        return 2, "All checks passed"  # ровно та ловушка 09.09

    ok, detail = await ops.push_resolved_base_merge(
        str(repo), "develop", "task-1233/probe", 1233, resolutions, _red, pinned, files
    )
    assert not ok and "код возврата 2" in detail, (
        "хвост вывода не доказательство — судим по коду возврата"
    )
    assert _tip(repo, "task-1233/probe") == before, "красная валидация не пушит ничего"

    async def _green(_path):
        return 0, "ok"

    ok, sha = await ops.push_resolved_base_merge(
        str(repo),
        "develop",
        "task-1233/probe",
        1233,
        resolutions,
        _green,
        pinned,
        files,
    )
    assert ok, sha
    assert _tip(repo, "task-1233/probe") != before, "зелёная валидация обновляет ветку"
    _run_git("git", "fetch", "-q", "origin", cwd=repo)
    import subprocess

    merged = subprocess.run(
        ["git", "show", "origin/task-1233/probe:tests_suite.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "<<<<<<<" not in merged
    assert merged.index("test_from_the_branch") < merged.index("test_from_the_base"), (
        "авторский блок остаётся первым: это и сохраняет его смещение в диффе"
    )
    # И наблюдение, ради которого этот тест написан на настоящем git, а не на
    # двойнике: сырой `git diff base...tip` после мержа базы НЕ совпадает с
    # прежним, даже когда авторская работа буква в букву та же. Меняются
    # строка index (блоб базы стал другим) и смещения ханков. Поэтому
    # сохранение вердикта в этом пути стоит не на сравнении диффов, а на том,
    # что коммит сделал сам гейт, — и он перезакрепляет коммит сдачи.
    author_before = await ops.branch_diff(str(repo), "develop", before)
    author_after = await ops.branch_diff(
        str(repo), "develop", _tip(repo, "task-1233/probe")
    )
    assert not base_merge.author_diff_unchanged(author_before, author_after)[0], (
        "если это когда-нибудь совпадёт — сравнение диффов станет годным и "
        "здесь, и перезакрепление коммита можно будет снять"
    )


def _tip(repo, branch: str) -> str:
    import subprocess

    _run_git("git", "fetch", "-q", "origin", cwd=repo)
    return subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ---- находки машинного ревью #345 по сдаче №1 (#1233) ----


async def test_the_automerge_anchors_on_the_pinned_commit_not_the_branch_tip(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Автомерж строит дерево на ЗАКРЕПЛЁННОМ коммите, а не на вершине ветки.

    Находка ee0c1eb000fe012b, и она была настоящей. Между сверкой с одобрением
    и этим шагом стоят проба CI, условие стопки и сам отказ мержа; в это окно в
    ветку может лечь чужой пуш. Пока автомерж брал ``origin/<branch>``, этот
    пуш попадал в слитый коммит И в перезакрепление — то есть человеческий
    вердикт переезжал на код, которого человек не видел.
    """
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(return_value=({}, ""))
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, validation_commands='["uv run pytest -q"]')
    await db.commit()
    pinned = dict(await repo.get_task(db, task_id))["submission_sha"]
    assert pinned, "сцена бессмысленна без закрепления"

    await _report_done(db, task_id)

    g.base_merge_conflicts.assert_awaited_once()
    assert g.base_merge_conflicts.await_args.args[4] == pinned, (
        "проба конфликта задаётся о закреплённом коммите: вершина ветки могла "
        "уехать, и мержить её значило бы взять неодобренный код"
    )


async def test_an_unpinned_submission_gets_no_automerge_at_all(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Нет закрепления — нет якоря, и вершину ветки автомерж не берёт (#1233)."""
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(return_value=({}, ""))
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, submission_sha="")
    await db.commit()

    await _report_done(db, task_id)

    g.base_merge_conflicts.assert_not_awaited()
    feed = await _feed(db, task_id)
    assert "не закреплён" in feed, "причина отказа называется, а не молчит"


async def test_an_automerge_without_a_commit_never_wipes_the_pin(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Успех с пустым sha не стирает закрепление (находка 66436b8dfb8ce18b).

    Пустая строка в submission_sha читается гейтом как «сверка не проводилась»,
    и доставка идёт БЕЗ неё (#572). Тихая потеря закрепления опаснее
    несостоявшегося автомержа, поэтому здесь зовут человека.
    """
    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(
        return_value=({"tests/test_review_dispatch.py": _TAIL_CONFLICT}, "")
    )
    g.push_resolved_base_merge = AsyncMock(return_value=(True, "   "))
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, validation_commands='["uv run pytest -q"]')
    await db.commit()
    pinned = dict(await repo.get_task(db, task_id))["submission_sha"]

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["submission_sha"] == pinned, (
        "закрепление осталось на месте: пустое закрепление снимает сверку с "
        "одобрением, а её снимать никто не просил"
    )
    feed = await _feed(db, task_id)
    assert "коммит слитой ветки не назван" in feed


async def test_a_path_both_sides_created_is_not_a_tail_addition() -> None:
    """add/add одного пути — не хвостовые добавления (находка db22bf1440791d71).

    Воспроизведено на настоящем git: ветка и база завели каждая свой thing.py,
    diff3 дал ПУСТОГО общего предка и ничего до маркера, класс выходил
    «хвостовые добавления», а «оставить оба» склеивало два целых файла — в
    Python побеждает последнее определение, то есть версия базы молча
    переписывала авторскую. Пустой предок доказывает «ничего не переписано»
    только тогда, когда переписывать БЫЛО что: эти строки стоят над маркером.
    """
    from hub.services import base_merge

    add_add = (
        "<<<<<<< HEAD\n"
        "def handle(x):\n"
        "    return x + 1\n"
        "||||||| merged common ancestors\n"
        "=======\n"
        "def handle(x):\n"
        "    return x * 100\n"
        ">>>>>>> origin/develop\n"
    )
    kind, why = base_merge.classify_conflict(add_add)
    assert kind == base_merge.UNRESOLVABLE, (
        "склейка двух целых файлов не бывает «ничего не переписано»"
    )
    assert "завели этот путь заново" in why
    assert base_merge.resolve_tail_additions(add_add) is None
    # А настоящий хвост к существующему файлу разрешается по-прежнему.
    assert base_merge.classify_conflict(_TAIL_CONFLICT)[0] == base_merge.TAIL_ADDITIONS


async def test_the_gate_path_runs_the_validation_and_stops_on_a_red_code(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Красный код возврата останавливает ГЕЙТ, а не только git_ops (#1233).

    Находка 43e5205bf49151b7: AC-3 подменял push_resolved_base_merge двойником
    и проверял лишь, что callback не None. «Callback передан» и «callback
    вызван, а его код возврата решил судьбу доставки» — разные утверждения, и
    ограничение постановки (09.09 дважды видели «All checks passed» при коде 2)
    про второе. Здесь двойник ВЫЗЫВАЕТ переданную валидацию.
    """
    seen: dict[str, object] = {}

    async def _push(
        repo, base, branch, task_id, resolutions, validate=None, tip="", probed=None
    ):
        seen["rc"], seen["log"] = await validate("/tmp/basemerge")
        seen["tip"] = tip
        seen["probed"] = probed
        return False, f"валидация после автомержа упала (код возврата {seen['rc']})"

    g = _seeing(monkeypatch, "approved0commit", merged=False)
    g.base_merge_conflicts = AsyncMock(
        return_value=({"tests/test_review_dispatch.py": _TAIL_CONFLICT}, "")
    )
    g.push_resolved_base_merge = _push
    monkeypatch.setattr(
        validation_run,
        "default_validation_runner",
        AsyncMock(return_value=(2, "All checks passed")),
    )
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, validation_commands='["uv run pytest -q"]')
    await db.commit()
    pinned = dict(await repo.get_task(db, task_id))["submission_sha"]

    await _report_done(db, task_id)

    assert seen.get("rc") == 2, "валидация обязана быть ВЫЗВАНА, а не только передана"
    assert seen.get("log") == "All checks passed", (
        "ровно ловушка 09.09: зелёный хвост при красном коде возврата"
    )
    assert seen.get("tip") == pinned, "и мержит она закреплённое, а не вершину"
    # Находка 2327bd9255c601cc: пуш сверяет конфликт с ТОЙ ЖЕ пробой, по
    # которой считалось разрешение. Гейт, забывший её передать, превращал бы
    # каждый хвостовой автомерж в отказ «не с чем сверить» — молча и навсегда.
    assert seen.get("probed") == {"tests/test_review_dispatch.py": _TAIL_CONFLICT}, (
        "гейт обязан отдать пушу ту пробу, по которой посчитано разрешение"
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "красный код возврата ведёт к человеку, а не к доставке"
    )
    assert task["submission_sha"] == pinned, "провалившийся автомерж не перезакрепляет"
    feed = await _feed(db, task_id)
    assert "Автомерж базы" not in feed, "несостоявшийся автомерж не пишет об успехе"


async def test_a_push_in_the_await_window_never_rides_the_verdict_in(
    tmp_path,
) -> None:
    """НАСТОЯЩИЙ git: неодобренный пуш не попадает под чужой вердикт (#1233).

    Находка ee0c1eb000fe012b, воспроизведённая делом ДО починки: автомерж брал
    ``origin/<branch>``, поэтому коммит, легший в ветку после одобрения,
    оказывался предком слитого — и предком того самого sha, который уезжал в
    submission_sha. Человеческий вердикт переезжал на код, которого человек не
    видел. Цена ошибки здесь выше всех прочих находок вместе, поэтому проверка
    на настоящем git, а не на двойнике.
    """
    import subprocess

    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    approved = _tip(repo, "task-1233/probe")

    ops = GitOpsIntegration()
    files, why = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, approved
    )
    assert files, why
    resolutions, _ = base_merge.plan_resolution(files)
    assert resolutions

    # ОКНО ОЖИДАНИЯ: между пробой и пушем в ветку ложится чужая работа.
    _run_git("git", "checkout", "-q", "task-1233/probe", cwd=repo)
    _run_git("git", "reset", "-q", "--hard", "origin/task-1233/probe", cwd=repo)
    (repo / "unreviewed.py").write_text("SECRET = 'этого никто не одобрял'\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-q", "-m", "push in the await window", cwd=repo)
    _run_git("git", "push", "-q", "origin", "task-1233/probe", cwd=repo)
    intruder = _tip(repo, "task-1233/probe")
    assert intruder != approved

    async def _green(_path):
        return 0, "ok"

    ok, detail = await ops.push_resolved_base_merge(
        str(repo),
        "develop",
        "task-1233/probe",
        1233,
        resolutions,
        _green,
        approved,
        files,
    )
    assert not ok, (
        "ветка ушла с одобренного коммита — автомерж обязан отказать, а не "
        "слить и перезакрепить вердикт на неодобренное"
    )
    assert "ушла с закреплённого коммита" in detail
    assert _tip(repo, "task-1233/probe") == intruder, (
        "отказавший автомерж не двигает ветку и ничего не затирает"
    )
    carries = subprocess.run(
        ["git", "cat-file", "-e", f"{approved}:unreviewed.py"],
        cwd=repo,
        capture_output=True,
    )
    assert carries.returncode != 0, "неодобренного кода нет в закреплённом коммите"


async def test_an_unreadable_merge_commit_is_a_refusal_not_an_empty_pin(
    tmp_path, monkeypatch
) -> None:
    """(True, "") не бывает: пустое закрепление хуже несостоявшегося автомержа.

    Находка 66436b8dfb8ce18b. ``rev-parse HEAD`` читался без кода возврата, и
    его молчание превращалось в успех с пустым sha. Пустота уезжала в
    submission_sha, а пустое закрепление гейт читает как «сверка с одобрением
    не проводилась» и доставляет БЕЗ неё (#572) — то есть неудача чтения
    снимала бы проверку, ради которой закрепление и заведено.
    """
    from hub.integrations import git_ops as git_ops_mod
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")
    files, why = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, pinned
    )
    assert files, why
    resolutions, _ = base_merge.plan_resolution(files)

    real_git = git_ops_mod._git

    async def _blind_rev_parse(*args, **kw):
        if args[:2] == ("rev-parse", "HEAD"):
            return 128, "", "fatal: ambiguous argument 'HEAD'"
        return await real_git(*args, **kw)

    monkeypatch.setattr(git_ops_mod, "_git", _blind_rev_parse)

    async def _green(_path):
        return 0, "ok"

    ok, detail = await ops.push_resolved_base_merge(
        str(repo),
        "develop",
        "task-1233/probe",
        1233,
        resolutions,
        _green,
        pinned,
        files,
    )

    assert not ok, "неудача чтения коммита — это отказ, а не успех"
    assert detail.strip(), "у отказа всегда есть названная причина"
    assert "закрепление не трогаем" in detail
    assert _tip(repo, "task-1233/probe") == pinned, (
        "ветка не сдвинулась: пушить то, чего не смогли назвать, автомерж не станет"
    )


async def test_a_branch_rewound_under_the_automerge_is_not_clobbered(
    tmp_path,
) -> None:
    """Аренда пуша: ветку, уехавшую ПОСЛЕ пробы, автомерж не затирает (#1233).

    Дерево строится на закреплённом коммите, поэтому обычный пуш и так отказал
    бы любому коммиту ПОВЕРХ него — не fast-forward. Аренда закрывает другой
    случай, который простой пуш пропустил бы молча: ветку откатили НАЗАД (чужой
    force-push в окно ожидания). Слитый коммит тогда оказывается потомком новой
    вершины, обычный пуш прошёл бы как fast-forward и стёр бы этот откат.
    Момент отката подстроен точно: ``validate`` вызывается между подготовкой
    дерева и пушем, то есть ровно в то окно, которое аренда и стережёт.
    """
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")
    rewound_to = _run_git_out("git", "rev-parse", f"{pinned}^", cwd=repo)

    files, why = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, pinned
    )
    assert files, why
    resolutions, _ = base_merge.plan_resolution(files)

    async def _green_but_someone_rewinds(_path):
        # Окно между подготовкой дерева и пушем — здесь и живёт гонка.
        _run_git(
            "git",
            "push",
            "-q",
            "--force",
            "origin",
            f"{rewound_to}:refs/heads/task-1233/probe",
            cwd=repo,
        )
        return 0, "ok"

    ok, detail = await ops.push_resolved_base_merge(
        str(repo),
        "develop",
        "task-1233/probe",
        1233,
        resolutions,
        _green_but_someone_rewinds,
        pinned,
        files,
    )

    assert not ok, (
        "ветку откатили под нами — пуш обязан отказать, а не пройти "
        "fast-forward'ом поверх чужого отката"
    )
    assert _tip(repo, "task-1233/probe") == rewound_to, (
        "чужой откат остался на месте: автомерж ничего не затёр"
    )


def _run_git_out(*args: str, cwd) -> str:
    import subprocess

    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


async def _stranded_base(db: aiosqlite.Connection, branch: str) -> int:
    """Задача, принятая человеком без доставки: свип нашёл её PR открытым.

    Состояние берётся КОНСТАНТОЙ из hub.services.delivery_state, а не строкой
    в тесте. Найдено машинным ревью сдачи №6: AC-2 и AC-3 подставляли
    state='merged' — токен, которого свип не пишет ВООБЩЕ (pr_state=merged он
    мапит в 'delivered'). Запрос — allowlist на 'pr_open', поэтому любая
    выдумка мимо него проходила, и тесты оставались зелёными на состоянии,
    которого продакшен не порождает. Замерено мутацией: замена условия на
    ``state IN ('pr_open','delivered')`` — то есть превращение КАЖДОГО
    доставленного основания в застрявшее, остановка всех доставок разом —
    не уронила ни одного из 112 тестов. Импорт делает переименование токена
    ошибкой импорта, а не молча зелёным тестом.
    """
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title="Accepted, never delivered"))
    await repo.update_task(db, tv.id, status="completed", branch=branch, pr_number=3)
    await repo.record_delivery_discrepancy(
        db,
        task_id=tv.id,
        state=PR_OPEN,
        reason="PR #3 открыт и не смержен — работа не в базовой ветке",
        pr_number=3,
        delivery_path="none",
    )
    await db.commit()
    return tv.id


async def test_an_accepted_but_undelivered_base_calls_a_human(
    db: aiosqlite.Connection,
) -> None:
    # AC-1: мерж не выполняется, и задача уходит к ЧЕЛОВЕКУ с названным
    # номером основания — не в транзитное удержание, которое здесь было бы
    # обещанием, которое хаб не может сдержать.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "ждать нечего: конвейер к принятой задаче не вернётся"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body
    assert "task-1138/eslint-debt" in body


async def test_a_closed_unmerged_base_also_calls_a_human(
    db: aiosqlite.Connection,
) -> None:
    # REPRO for the P1 finding on fe32ee25 (hub/repository.py:544): a base
    # task accepted as completed whose PR was closed WITHOUT merging is, per
    # delivery_state.task_delivery's own docstring, "work dropped on purpose"
    # — delivery_path="none", exactly like pr_open. Its commits are still
    # ancestors of any branch drawn from it before the close, and a squash
    # merge of the dependent branch would carry them into the base branch
    # under the dependent's number — the same #1183-over-#1175 shape AC-1
    # exists to prevent. list_undelivered_completed_branch_tasks only reads
    # state='pr_open', so this base never enters ``stranded`` and the gate
    # currently reads the stack as clear.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, "task-901/closed-without-merge")
    await repo.record_delivery_discrepancy(
        db,
        task_id=base_id,
        state=PR_CLOSED,
        reason="PR #3 закрыт без мержа — работу свернули намеренно",
        pr_number=3,
        delivery_path="none",
    )
    await db.commit()

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "закрытый без мержа PR тоже никогда не доедет до базовой ветки сам: "
        "мерж сейчас унёс бы отвергнутую работу основания под нашим номером"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body
    # Cursor #385 (fc420e738b347d00): закрытый PR не называется открытым.
    assert "её PR закрыт без мержа" in body, body
    assert "её PR открыт" not in body, body


async def test_a_closed_base_whose_branch_is_gone_does_not_hold_the_project(
    db: aiosqlite.Connection,
) -> None:
    # Cursor #385 (4a97669554fc9e13), high; решение стюарда 15.09, вариант 1.
    # pr_closed свип не переспрашивает, а ветку свёрнутой работы никто не
    # вернёт. Звать человека на такой строке значило бы ставить в
    # needs_decision КАЖДУЮ доставку проекта без своей явной стопки, без
    # отпирающего события. Исход — мерж с алертом, называющим задачу.
    from hub.integrations.protocols import StackProbeOutcome

    dead = "task-901/closed-and-deleted"
    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
        details=dead,
    )
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, dead)
    await repo.record_delivery_discrepancy(
        db,
        task_id=base_id,
        state=PR_CLOSED,
        reason="PR #3 закрыт без мержа — работу свернули намеренно",
        pr_number=3,
        delivery_path="none",
    )
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] != "needs_decision", (
        "одна свёрнутая задача с удалённой веткой не должна звать человека "
        "на каждой доставке проекта"
    )
    assert g.merge_pr.await_count == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body and dead in body, (
        "мерж не молчаливый: алерт называет, что проверить было нечем"
    )
    # Cursor #391 (d560bd210dee88dc): проба ОТВЕТИЛА (ветки нет), и алерт не
    # говорит «хаб не получил ответа» / «плагин мог и проверить».
    assert "Плагин мог и проверить" not in body, body
    assert "ответа, на который можно опереться, хаб не получил" not in body, body
    assert "PR закрыт без мержа" in body, body


async def test_a_closed_gone_base_does_not_mask_a_plain_unknown(
    db: aiosqlite.Connection,
) -> None:
    # Обратная сторона: свёрнутая база ниже обычного unknown. Моргнувший git
    # на соседней строке по-прежнему ждёт, а не мержит с алертом только
    # потому, что рядом лежит закрытая база. Мутация «closed_gone выше
    # unknown в _first_of» роняет этот тест.
    from hub.integrations.protocols import StackProbeOutcome, StackProbeResult

    g = _git(CIProbeOutcome.passed, merged=True)
    dead = "task-901/closed-and-deleted"
    live = "task-1175/image-capture"

    async def _probe(_branch, other_branch, **_kw):
        if other_branch == dead:
            return StackProbeResult(
                outcome=StackProbeOutcome.unavailable,
                reason="ref_unresolved",
                details=dead,
            )
        return StackProbeResult(
            outcome=StackProbeOutcome.unavailable,
            reason="rev_list_failed",
            details=f"rc=1/0 for {other_branch}",
        )

    g.branch_stacking_probe = _probe
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, dead)
    await repo.record_delivery_discrepancy(
        db,
        task_id=base_id,
        state=PR_CLOSED,
        reason="PR #3 закрыт без мержа — работу свернули намеренно",
        pr_number=3,
        delivery_path="none",
    )
    await _base_task_in_review(db, live)
    await db.commit()

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "непроверенная живая строка — повторяемое ожидание, как и было"
    )


async def test_a_delivered_base_does_not_hold_anything(
    db: aiosqlite.Connection,
) -> None:
    # AC-2: то же основание, но доставленное — свип записал не pr_open.
    # Поведение прежнее, ни удержания, ни вопроса.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, "task-1138/eslint-debt")
    await repo.record_delivery_discrepancy(
        db,
        task_id=base_id,
        state=DELIVERED,
        reason="PR #3 смержен",
        pr_number=3,
        delivery_path="outside_gate",
    )
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "у основания стопки нет ни того, кого ждать, ни того, что решать"
    )
    assert g.merge_pr.await_count == 1
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1


async def test_delivered_tasks_are_not_candidates(db: aiosqlite.Connection) -> None:
    # AC-3, переформулирован против постановки после замера. В постановке было
    # «попадание определяется записью в pipeline_merges». Это оказалось неверно:
    # pipeline_merges говорит, мержил ли ХАБ, и ручной мерж не оставляет строки —
    # undelivered_blockers документирует это как допустимое ровно потому, что
    # ГЕЙТ, КОТОРЫЙ ОНА КОРМИТ, ADVISORY. Этот — нет. Признак взят из
    # delivery_discrepancies, который спрашивает состояние самого PR.
    #
    # Плюс вторая причина, которой в постановке не было: после доставки гейт
    # УДАЛЯЕТ ветку, а tasks.branch остаётся заполненной. Возьми мы всех
    # completed в кандидаты — сотни неразрешимых ссылок дали бы unavailable,
    # который по правилу #1186 старше clear, и гейт встал бы навсегда.
    delivered = await _stranded_base(db, "task-900/long-since-merged")
    await repo.record_delivery_discrepancy(
        db,
        task_id=delivered,
        state=DELIVERED,
        reason="PR #3 смержен",
        pr_number=3,
        delivery_path="outside_gate",
    )
    # PR_CLOSED is NOT a second exclusion here (fix for the P1 finding on
    # fe32ee25, see repo.list_undelivered_completed_branch_tasks docstring):
    # a PR closed without merging still leaves the base's commits as
    # ancestors of any branch drawn from it, and the stacking gate is asking
    # a safety question ("would merging now carry undelivered commits
    # forward"), not a timing question ("when will this arrive"). A prior
    # revision of this test asserted the opposite and was wrong to.
    # tests/test_delivery_gate.py::test_a_closed_unmerged_base_also_calls_a_human
    # covers the closed case end to end through the gate.
    closed = await _stranded_base(db, "task-901/closed-without-merge")
    await repo.record_delivery_discrepancy(
        db,
        task_id=closed,
        state=PR_CLOSED,
        reason="PR #3 закрыт без мержа",
        pr_number=3,
        delivery_path="none",
    )
    await db.commit()

    rows = [
        dict(r)
        for r in await repo.list_undelivered_completed_branch_tasks(
            db, exclude_task_id=999
        )
    ]

    ids = [r["id"] for r in rows]
    assert delivered not in ids, (
        "доставленное основание не кандидат, и признак — состояние PR, не статус"
    )
    assert closed in ids, (
        "закрытый без мержа PR — коммиты всё равно не в базовой ветке, "
        "и они всё равно предки любой ветки, отведённой от основания"
    )


async def test_an_unanswerable_pr_state_is_not_a_candidate(
    db: aiosqlite.Connection,
) -> None:
    # Названное слепое пятно, а не забытый случай. state=unknown значит, что
    # провайдер не ответил — это не «не доставлено» (#725). В кандидаты такие
    # строки не берутся сознательно: их ветки обычно давно удалены, проба
    # ответила бы unavailable, а он по правилу #1186 старше clear — то есть
    # гейт встал бы навсегда на горстке древних задач. Тест держит именно
    # ЭТОТ выбор, чтобы следующий читатель не принял его за недосмотр.
    unanswered = await _stranded_base(db, "task-878/flywheel")
    await repo.record_delivery_discrepancy(
        db,
        task_id=unanswered,
        state=UNKNOWN,
        reason="состояние PR узнать не удалось: провайдер не ответил",
        pr_number=443,
        delivery_path="unknown",
    )
    await db.commit()

    rows = [
        dict(r)
        for r in await repo.list_undelivered_completed_branch_tasks(
            db, exclude_task_id=999
        )
    ]

    assert unanswered not in [r["id"] for r in rows]


async def test_a_live_pr_open_row_is_still_re_asked_past_the_lookback(
    db: aiosqlite.Connection,
) -> None:
    """Строка, на которой стоит необратимый гейт, не имеет права замёрзнуть.

    Найдено машинным ревью сдачи №6 и воспроизведено зондом до починки.
    ``list_undelivered_completed_branch_tasks`` берёт ЛЮБУЮ живую строку
    pr_open, а свип переспрашивал только задачи внутри
    DELIVERY_SCAN_LOOKBACK_DAYS. За окном строка застывала на том, что
    провайдер сказал в последний раз. Доставь основание руками — смержи PR,
    удали ветку — и исправить строку уже нечему: свип о ней не спрашивает,
    гейт ей верит.

    Дальше это не одна задержанная доставка. Мёртвая ссылка на кандидате
    уходит в ``_stranded_with_a_dead_ref``, тот исход НЕ транзитный, а
    ``list_pair_tasks_awaiting_delivery`` берёт только running — значит
    повторить некому. И обход спрашивает про эту строку у КАЖДОЙ задачи
    проекта: один ископаемый ряд запирает доставки всего проекта, и события,
    которое бы его отперло, не существует.

    Зонд до починки: кандидат гейта — [id], список свипа — пусто.
    """
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title="Принята 40 дней назад"))
    await repo.update_task(
        db, tv.id, status="completed", branch="task-900/fossil", pr_number=7
    )
    await repo.record_delivery_discrepancy(
        db,
        task_id=tv.id,
        state=PR_OPEN,
        reason="PR #7 открыт и не смержен",
        pr_number=7,
        delivery_path="none",
    )
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now','-40 days'), "
        "updated_at = datetime('now','-40 days') WHERE id = ?",
        (tv.id,),
    )
    await db.commit()

    candidates = [
        dict(r)["id"]
        for r in await repo.list_undelivered_completed_branch_tasks(
            db, exclude_task_id=999
        )
    ]
    asked = [
        dict(r)["id"]
        for r in await repo.completed_tasks_awaiting_delivery(db, lookback_days=30)
    ]

    assert tv.id in candidates, "гейт этой строке верит"
    assert tv.id in asked, (
        "значит свип обязан её переспрашивать: строка, которую больше не "
        "проверяют, но на которую опирается необратимый отказ, уже не "
        "наблюдение, а память"
    )


async def _live_row(
    db: aiosqlite.Connection, *, title: str, pr: int, age_hours: int
) -> int:
    """Задача completed с живой строкой pr_open заданного возраста."""
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title=title))
    await repo.update_task(
        db, tv.id, status="completed", branch=f"task-{tv.id}/x", pr_number=pr
    )
    await repo.record_delivery_discrepancy(
        db,
        task_id=tv.id,
        state=PR_OPEN,
        reason=f"PR #{pr} открыт и не смержен",
        pr_number=pr,
        delivery_path="none",
    )
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now', ?), "
        "updated_at = datetime('now', ?) WHERE id = ?",
        (f"-{age_hours} hours", f"-{age_hours} hours", tv.id),
    )
    await db.commit()
    return tv.id


async def test_the_limit_does_not_cut_off_the_oldest_live_row(
    db: aiosqlite.Connection,
) -> None:
    """Потолок выборки не имеет права всегда отрезать одни и те же строки.

    Найдено машинным ревью сдачи №7. Сдача №6 сняла с живых pr_open временное
    окно и поставила их первыми — но ВНУТРИ них порядок остался
    ``completed_at DESC``, то есть фиксированным. Живых строк больше потолка
    (по умолчанию 100, свип своего не передаёт) — и за срез уходят всегда одни
    и те же: самые старые. Это ровно те ископаемые, которые после ручного
    merge + delete ветки дают мёртвую ссылку и нетранзитный отказ на весь
    проект. Фиксированный порядок такую строку не задерживает, а исключает
    навсегда.

    Числа взяты с прода (hub_undelivered_completed, 10.09.2026): живая строка
    задачи #1138 возрастом 165 ч и строка #878 возрастом 467 ч, которая
    становится живой ровно тем переходом, ради которого живут #1214/#1215, —
    провайдер заговорил и сказал pr_open.

    Мутация: верни ``completed_at DESC`` внутри живых строк — упадёт здесь.
    """
    fresh = await _live_row(db, title="#1138 eslint", pr=3, age_hours=165)
    fossil = await _live_row(db, title="#878 маховик", pr=443, age_hours=467)

    gate = [
        dict(r)["id"]
        for r in await repo.list_undelivered_completed_branch_tasks(
            db, exclude_task_id=999
        )
    ]
    assert {fresh, fossil} <= set(gate), "гейт стоит на обеих живых строках"

    asked = [
        dict(r)["id"] for r in await repo.completed_tasks_awaiting_delivery(db, limit=1)
    ]
    assert asked == [fossil], (
        "при потолке меньше числа живых строк свип обязан начинать со САМОЙ "
        f"СТАРОЙ (467 ч), а не с самой молодой: {asked}"
    )


async def test_live_rows_rotate_so_none_is_starved(
    db: aiosqlite.Connection,
) -> None:
    """Порядок живых строк — ротация, а не приоритет.

    Спросили строку — она уходит в конец очереди, и следующий свип берёт
    другую. Значит при R живых строках и потолке L каждая опрашивается не
    позже чем через ceil(R/L) свипов: доверие гейта к строке протухает за
    ограниченное время, а не никогда.

    Мутация: убери ключ ``checked_at`` — свип будет вечно спрашивать одну и ту
    же строку, и тест упадёт на втором шаге.
    """
    first = await _live_row(db, title="строка A", pr=11, age_hours=400)
    second = await _live_row(db, title="строка B", pr=12, age_hours=300)
    # checked_at пишется с точностью до секунды, поэтому в тесте развожу его
    # явно — иначе обе строки попадут в одну секунду и сравнивать будет нечего.
    await db.execute(
        "UPDATE delivery_discrepancies SET checked_at = datetime('now','-2 hours') "
        "WHERE task_id = ?",
        (first,),
    )
    await db.execute(
        "UPDATE delivery_discrepancies SET checked_at = datetime('now','-1 hours') "
        "WHERE task_id = ?",
        (second,),
    )
    await db.commit()

    asked = [
        dict(r)["id"] for r in await repo.completed_tasks_awaiting_delivery(db, limit=1)
    ]
    assert asked == [first], f"первой идёт та, которую спрашивали давнее: {asked}"

    # свип переспросил её и записал ответ — checked_at подвинулся
    await repo.record_delivery_discrepancy(
        db, task_id=first, state=PR_OPEN, reason="переспросили", pr_number=11
    )
    asked_next = [
        dict(r)["id"] for r in await repo.completed_tasks_awaiting_delivery(db, limit=1)
    ]
    assert asked_next == [second], (
        "после ответа строка обязана уйти в конец очереди, иначе это не "
        f"ротация, а тот же вечный приоритет: {asked_next}"
    )


async def test_an_unanswered_row_past_the_lookback_is_not_re_asked_forever(
    db: aiosqlite.Connection,
) -> None:
    """Отсрочка дана ровно pr_open, и это выбор, а не побочный эффект.

    unknown в кандидаты гейта не входит вовсе (см. соседний тест), поэтому его
    несвежесть ничего не стоит. А переспрашивать вечно строки, на которые
    провайдер уже отказался отвечать, — это сетевой вызов каждый свип за ответ,
    которого не будет. Тест держит границу отсрочки: расширь её на unknown, и
    он упадёт.
    """
    from hub.models import TaskCreate
    from hub.services import create_task

    tv = await create_task(db, TaskCreate(title="Провайдер молчал 40 дней назад"))
    await repo.update_task(
        db, tv.id, status="completed", branch="task-878/flywheel", pr_number=8
    )
    await repo.record_delivery_discrepancy(
        db,
        task_id=tv.id,
        state=UNKNOWN,
        reason="состояние PR узнать не удалось: провайдер не ответил",
        pr_number=8,
        delivery_path="unknown",
    )
    await db.execute(
        "UPDATE tasks SET completed_at = datetime('now','-40 days'), "
        "updated_at = datetime('now','-40 days') WHERE id = ?",
        (tv.id,),
    )
    await db.commit()

    asked = [
        dict(r)["id"]
        for r in await repo.completed_tasks_awaiting_delivery(db, lookback_days=30)
    ]

    assert tv.id not in asked


async def test_the_undeliverable_base_is_not_worded_as_a_wait(
    db: aiosqlite.Connection,
) -> None:
    # AC-4: у stacked_base сказано «ждём доставки #N» — там это правда.
    # Здесь ждать нечего, и текст обязан говорить именно это, иначе читатель
    # уйдёт ждать событие, которого не будет.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "Ждём доставки" not in body, "обещание ожидания, которого не будет"
    assert "Ждать нечего" in body
    assert "Решение за человеком" in body


async def test_a_stranded_task_built_on_top_of_us_does_not_block_us(
    db: aiosqlite.Connection,
) -> None:
    """Застрявшая задача может стоять ПОВЕРХ нас, а не под нами.

    Названо измерением gate-semantics при ревью #1204. Предикат стопки
    симметричен, поэтому «застряло» и «мы на нём стоим» — разные вопросы, и
    второй решает ancestry. Если человек принял без доставки ту задачу, что
    отведена от НАШЕЙ ветки, мы ни на чём не стоим: мерж унесёт только наши
    коммиты. Отказ здесь запер бы доставимую половину пары ради недоставимой
    и предложил бы «отвязать» ветку, которая ни к чему не привязана.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_ancestor")
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-B/accepted-on-top-of-us")

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "мы основание: ждать нечего и решать нечего, мержимся первыми"
    )
    assert g.merge_pr.await_count == 1


async def test_a_benign_stranded_row_does_not_end_the_walk(
    db: aiosqlite.Connection,
) -> None:
    """Застрявшая строка стоит в обходе ПЕРВОЙ — значит её «continue» несущий.

    Найдено машинным ревью сдачи №6 и воспроизведено мутацией до починки:
    ``if other_id in stranded: return found`` перед проверкой ancestry пережила
    82 теста. Соседний тест — «застрявшая задача поверх нас не блокирует нас» —
    её не ловит, потому что ``_stacking_gate_step`` САМ разбирает
    head_is_ancestor раньше вопроса о застревании: исход у той пары одинаковый
    хоть с ранним возвратом, хоть без.

    Вред не у той пары, а у обхода. Застрявшие строки кладутся В НАЧАЛО списка
    (это сделано намеренно: иначе ближнее обычное основание маскировало бы
    застрявшее). Значит застрявшая задача, стоящая ПОВЕРХ нас, встречается
    первой ВСЕГДА, и ранний возврат на ней означал бы, что настоящее основание
    ПОД нами не спрашивают вообще — мы мержим и уносим его несмерженные
    коммиты под своим номером. Это ровно тот инцидент 06.09, ради которого всё
    условие написано, только через собственную оптимизацию.

    Здесь: #B принята без доставки и отведена ОТ нашей ветки (мы её основание),
    #A в review и наша ветка стоит НА ней. Обход обязан пройти мимо первой и
    упереться во вторую.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)

    async def _ancestry(branch: str, other_branch: str, repo: str | None = None) -> str:
        # Мы — основание для застрявшей #B и потомок настоящего основания #A.
        if other_branch == "task-B/stranded-on-top-of-us":
            return "head_is_ancestor"
        return "head_is_descendant"

    g.branch_ancestry = AsyncMock(side_effect=_ancestry)
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-B/stranded-on-top-of-us")
    real_base = await _base_task_in_review(db, "task-A/genuinely-under-us")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "настоящее основание в review — это удержание: оно доставится само"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{real_base}" in body, (
        "обход остановился на доброкачественной застрявшей строке и не дошёл "
        "до основания, которое действительно под нами"
    )
    assert "task-A/genuinely-under-us" in body


async def test_a_stranded_base_outranks_an_undetermined_order(
    db: aiosqlite.Connection,
) -> None:
    """Порядок двух проверок — решение, а не случайность, и оно закреплено.

    Найдено измерением test-adequacy: оба теста выше фиксируют ancestry в
    head_is_descendant, то есть проверяют лишь один из двух путей к этому
    исходу. Между тем застрявшее основание вполне может стоять с любым
    отношением — например указывать на тот же коммит. Если порядок проверок
    когда-нибудь поменяют местами, читатель получит «порядок мержа из истории
    не следует» вместо «ждать нечего, основание принято без доставки»: совет
    установить порядок там, где никакой порядок не поможет.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="same_tip")
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "Ждать нечего" in body
    assert "порядок мержа из истории не следует" not in body, (
        "у ветки, которую не доставят, порядок называть незачем"
    )


async def test_the_advisory_walk_does_not_see_stranded_bases(
    db: aiosqlite.Connection,
) -> None:
    """Единственная граница между двумя вопросами — и она была без теста.

    Застрявшие основания подмешиваются в обход ТОЛЬКО когда передан перечень
    статусов, то есть только на пути доставки. Advisory-потребители (сдача и
    бриф ревью) спрашивают другое — «работает ли кто-то поверх меня прямо
    сейчас», — и задача, которую уже приняли, к этому вопросу не относится.
    Снятие условия протащило бы её в подсказку человеку, и ни один из тестов
    выше этого бы не заметил: все они идут через гейт доставки.
    """
    from hub.integrations.protocols import StackProbeOutcome
    from hub.services import orchestration as orch

    _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")
    task = dict(await repo.get_task(db, task_id))

    advisory = await orch.assess_branch_stacking(db, task_id, task["branch"] or "")
    delivery = await orch.assess_branch_stacking(
        db,
        task_id,
        task["branch"] or "",
        statuses=orch.STACK_DELIVERY_STATUSES,
    )

    assert advisory.outcome == orch.STACK_CLEAR, (
        "принятая задача не входит в вопрос «кто работает поверх меня»"
    )
    assert advisory.as_advisory() is None
    assert delivery.outcome == orch.STACK_STACKED, (
        "тот же обход на пути доставки её видит — иначе тест выше проходил бы "
        "по причине, не имеющей отношения к границе"
    )
    assert delivery.base_can_deliver_itself is False


async def test_a_nearer_ordinary_base_does_not_mask_a_stranded_one(
    db: aiosqlite.Connection,
) -> None:
    """Обход отвечает первой найденной стопкой, значит порядок и есть приоритет.

    Трёхуровневая стопка: под текущей задачей ветка A (в review, доставится
    сама), а под A — ветка B, которую человек принял без доставки. Ветка
    текущей задачи транзитивно содержит немерженные коммиты обеих. Пока
    застрявшие строки шли в конце списка, ответом становилась A: читатель
    получал «ждём доставки #A» — молчаливый повтор, — а под ним лежало
    основание, которого не дождётся никто.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    ordinary = await _base_task_in_review(db, "task-A/still-in-review")
    stranded = await _stranded_base(db, "task-B/accepted-never-delivered")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert task["status"] == "needs_decision", (
        "нижнее основание не доставится никогда — это вопрос, а не ожидание"
    )
    assert f"#{stranded}" in body
    assert f"Ждём доставки #{ordinary}" not in body, (
        "ближнее основание не должно закрывать собой то, которого не дождаться"
    )


async def test_a_stranded_base_is_not_told_a_direction_git_never_confirmed(
    db: aiosqlite.Connection,
) -> None:
    """Не утверждать больше, чем хаб знает — второй раз в том же месте.

    Проверка застревания стоит выше вопроса о порядке мержа, значит она
    ловит и формы, где ancestry ничего не сказала. Текст при этом утверждал
    «ветка стоит на ветке задачи #N» безусловно — направленный факт, который
    git не подтверждал. Тот же перебор уже чинили в алерте о непроверенной
    стопке (#725), и он вернулся, потому что перестановка проверок расширила
    охват сообщения, а само сообщение осталось прежним.
    """
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(_git(CIProbeOutcome.passed, merged=True), StackProbeOutcome.stacked)
    g.branch_ancestry = AsyncMock(return_value="unknown")
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body, "номер основания назван в любом случае"
    assert "ветка стоит на ветке задачи" not in body, (
        "направление не подтверждено — значит и не называется"
    )
    assert "направление git не подтвердил" in body


async def test_a_stranded_base_with_a_dead_branch_calls_a_human(
    db: aiosqlite.Connection,
) -> None:
    # Найдено машинным ревью сдачи №2 (uid d670192f5bdb8a4d), high.
    # Строка «PR открыт» с мёртвой ссылкой попадала в обход как обычный
    # кандидат: проба отвечала unavailable, обход помнил unknown, а unknown
    # старше clear — и ЛЮБАЯ доставка того же проекта, у которой нет своей
    # явной стопки, уходила в транзитное удержание. Транзитное значит
    # молчаливое, а ветку принятой задачи никто не вернёт: удержание вечное.
    # Ровно тот кирпич, ради которого выше стоит пропуск чужих проектов.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
        details="task-1138/eslint-debt",
    )
    task_id = await _approved_pair_task(db)
    base_id = await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited(), "непроверенная стопка не повод мержить"
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "ни ждать (ветка не вернётся), ни мержить (это исходный инцидент) — "
        "остаётся позвать человека"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{base_id}" in body, "человеку называют, с какой задачей разбираться"
    assert "task-1138/eslint-debt" in body


async def test_the_unprobed_stranded_refusal_is_not_a_silent_wait(
    db: aiosqlite.Connection,
) -> None:
    # Половина вступления в множество — это попасть в кортеж транзитных
    # префиксов; вторая половина — НЕ попасть туда, когда ждать нечего.
    # Мутация «добавить префикс в TRANSIENT_GATE_PREFIXES» роняет этот тест,
    # и она же вернула бы молчаливое вечное удержание.
    from hub import services

    assert not services.UNPROBED_STRANDED_BASE_PREFIX.startswith(
        services.TRANSIENT_GATE_PREFIXES
    ), "удержание здесь было бы обещанием, которого хаб не может сдержать"
    assert not services.UNPROBED_STRANDED_BASE_PREFIX.startswith(
        services.STRANDED_BASE_PREFIX
    ), (
        "префикс не должен быть приставкой соседнего: рядом сравнивают "
        "через startswith, и приставка молча попала бы в чужую ветку разбора"
    )


async def test_a_definite_stack_outranks_an_unprobed_stranded_base(
    db: aiosqlite.Connection,
) -> None:
    # Непроверенный застрявший кандидат запоминается, а не возвращается сразу:
    # настоящая стопка — более полезный ответ, и она называет, чего ждать.
    # Проба отвечает по паре: мёртвой ссылке — unavailable, живому основанию —
    # stacked. Мутация «возвращать непроверенного немедленно» роняет тест.
    from hub.integrations.protocols import StackProbeOutcome, StackProbeResult

    g = _git(CIProbeOutcome.passed, merged=True)
    live = "task-1175/image-capture"
    dead = "task-1138/eslint-debt"

    async def _probe(_branch, other_branch, **_kw):
        if other_branch == dead:
            return StackProbeResult(
                outcome=StackProbeOutcome.unavailable,
                reason="ref_unresolved",
                details=dead,
            )
        return StackProbeResult(outcome=StackProbeOutcome.stacked, reason="scripted")

    g.branch_stacking_probe = _probe
    g.branch_ancestry = AsyncMock(return_value="head_is_descendant")
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, dead)
    live_id = await _base_task_in_review(db, live)

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "живое основание доставится само, поэтому это ожидание, а не решение"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{live_id}" in body and live in body


async def test_our_own_unresolvable_branch_still_waits(
    db: aiosqlite.Connection,
) -> None:
    # Точность правки, а не её широта. Не разрешилась НАША ветка — это про эту
    # машину, а не про ту задачу: клон догонит, следующий цикл ответит. Такой
    # случай обязан остаться повторяемым ожиданием, даже когда рядом лежит
    # застрявший кандидат. Мутация «считать любой ref_unresolved застрявшим»
    # роняет этот тест.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
        details="task-999/ours",
    )
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "не разрешилась своя ветка — лечится следующим циклом, а не человеком"
    )


async def test_an_unprobed_stranded_base_outranks_a_plain_unknown(
    db: aiosqlite.Connection,
) -> None:
    # Приоритет, а не украшение. Оба исхода значат «посмотреть не удалось»,
    # но повторяемый unknown ЖДЁТ молча, а этот зовёт человека и называет
    # задачу. Вернуть первый вместо второго — значит спрятать застрявшее
    # основание навсегда: ветка не вернётся, и ожидание не кончится.
    # Мутация «поменять порядок в _first_of» роняет этот тест; без него та
    # мутация не роняла ничего, то есть приоритет не был проверен вовсе.
    from hub.integrations.protocols import StackProbeOutcome, StackProbeResult

    g = _git(CIProbeOutcome.passed, merged=True)
    dead = "task-1138/eslint-debt"
    live = "task-1175/image-capture"

    async def _probe(_branch, other_branch, **_kw):
        if other_branch == dead:
            return StackProbeResult(
                outcome=StackProbeOutcome.unavailable,
                reason="ref_unresolved",
                details=dead,
            )
        return StackProbeResult(
            outcome=StackProbeOutcome.unavailable,
            reason="rev_list_failed",
            details=f"rc=1/0 for {other_branch}",
        )

    g.branch_stacking_probe = _probe
    task_id = await _approved_pair_task(db)
    stranded_id = await _stranded_base(db, dead)
    await _base_task_in_review(db, live)

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "молчаливое ожидание рядом с застрявшим основанием — это и есть "
        "способ его никогда не заметить"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert f"#{stranded_id}" in body


async def test_a_broken_clone_is_not_blamed_on_the_stranded_task(
    db: aiosqlite.Connection,
) -> None:
    # #1204, найдено машинным ревью сдачи №3: регрессия, внесённая предыдущей
    # правкой. Проба кладёт в details ВСЕ неразрешённые имена тройки — наше,
    # кандидата и базы. Первая версия спрашивала, есть ли кандидат СРЕДИ них,
    # поэтому сломанный клон, где не резолвится и наша ветка, объявлялся
    # «ветка той задачи исчезла» и уводил задачу к человеку. Сбой машины бьёт
    # всех кандидатов одинаково и проходит сам — он обязан остаться
    # повторяемым ожиданием. Мутация «вернуть проверку вхождением вместо
    # равенства» роняет этот тест.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="ref_unresolved",
        details="task-999/ours, task-1138/eslint-debt",
    )
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "не разрешилась и наша ветка — это про клон, а не про ту задачу"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ждать бесполезно" not in body, (
        "человеку не сообщают как факт то, чего хаб не установил"
    )


@pytest.mark.parametrize(
    "details",
    ["task-1138/eslint-debt: ls-remote rc=124: git молчит", "task-1138/eslint-debt"],
    ids=["as_the_probe_writes_it", "bare_name"],
)
async def test_an_unanswered_origin_is_not_blamed_on_the_stranded_task(
    db: aiosqlite.Connection, details: str
) -> None:
    # #1204, найдено машинным ревью сдачи №4. Проба теперь различает «origin
    # ответил, что ветки нет» (ref_unresolved) и «origin не ответил»
    # (remote_unreachable: таймаут, lock, auth). Второе — про эту машину, а не
    # про ту задачу: оно проходит само и обязано остаться повторяемым
    # ожиданием. Решает REASON, а не форма details: второй вариант кормит
    # голое имя кандидата, чтобы классификатор не держался на том, что
    # проба дописывает к имени текст ошибки. Мутация «зовём человека и на
    # remote_unreachable» роняет этот тест.
    from hub.integrations.protocols import StackProbeOutcome

    g = _probes(
        _git(CIProbeOutcome.passed, merged=True),
        StackProbeOutcome.unavailable,
        reason="remote_unreachable",
        details=details,
    )
    task_id = await _approved_pair_task(db)
    await _stranded_base(db, "task-1138/eslint-debt")

    await _report_done(db, task_id)

    g.merge_pr.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "origin не ответил — это повторяемое ожидание, а не вопрос человеку"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "ждать бесполезно" not in body
    assert "на origin её нет" not in body, (
        "«на origin её нет» утверждается только после ответа origin"
    )


# ---------------------------------------------------------------------------
# #1261: закрытый записанный PR доставляется только отчётом агента.
#
# pr_for_delivery (#767, #959) умеет менять закрытый/отсутствующий записанный
# номер на живой открытый PR той же ветки. Но у merge_before_completion три
# вызывающих, и правило применено перед одним — отчётом агента
# (orchestration._deliver_completed_pair_task, через _complete_without_review).
# Свип одобренных задач (poller._deliver_pair_task, #971 — СЕГОДНЯ основной
# путь доставки: #1172 и #1238 пришли им) и решение человека
# accept+pr_disposition=deliver (lifecycle.deliver_on_disposition, #1037) брали
# task["pr_number"] как есть. На #1204 (прод aa33d18, 13.09.2026, второй раз —
# тот же алерт был 09.09) записан закрытый #325, живой той же ветки — #332:
# свип получил "merge_failed: GitHub refused the merge" и увёл задачу в
# needs_decision, откуда отчёт агента запрещён (human_decision_required), а
# решение человека упёрлось бы в тот же #325 — тупик, не считая запрещённого
# ручного мержа.
#
# ВЫБОР ИСПОЛНИТЕЛЯ: разрешение PR — перед КАЖДЫМ вызовом
# merge_before_completion (resolve_delivery_pr, тонкая обёртка над
# pr_for_delivery), а не внутри самого гейта. Причина: путь отчёта агента уже
# зовёт pr_for_delivery САМ, на шаг выше (_complete_without_review, ради
# ensure_delivery_pr, #967) — протолкнуть разрешение внутрь гейта означало бы
# спросить провайдера о том же PR дважды за один отчёт. pr_for_delivery
# остаётся единственной копией правила #959; resolve_delivery_pr — второй
# вход в неё же, а не вторая логика.
# ---------------------------------------------------------------------------


async def test_the_delivery_sweep_replaces_a_closed_recorded_pr(
    db: aiosqlite.Connection,
) -> None:
    """AC-1. Живой случай #1204: записан #325 (CLOSED), у ветки открыт #332.

    На неисправленном коде (poller._deliver_pair_task зовёт
    merge_before_completion с task["pr_number"] как есть) этот тест красный:
    свип пытается мержить закрытый #325, GitHub отказывает, задача уходит в
    needs_decision с алертом "merge_failed: GitHub refused the merge" вместо
    доставки через #332.
    """
    g = _git_with_state("closed", found=332)
    task_id = await _approved_pair_task(db, pr_number=325)
    await repo.update_task(
        db, task_id, branch="task-1204/undelivered-base-calls-a-human"
    )
    await db.commit()

    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 332, "живой PR ветки записан на задачу"
    assert g.merge_pr.await_args.args[0] == 332, "и доставляется именно он"
    assert task["status"] == "completed"
    g.pr_for_branch.assert_awaited()
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#325" in feed and "#332" in feed, "оба номера названы в ленте"
    assert "merge_failed" not in feed


async def test_a_human_deliver_decision_replaces_a_closed_recorded_pr(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-2. Тот же случай #1204, но задача уже в needs_decision и человек
    принимает её с pr_disposition=deliver.

    На неисправленном коде (lifecycle.deliver_on_disposition зовёт
    merge_before_completion с task["pr_number"] как есть) тест красный: гейт
    пытается мержить закрытый #325 и отказывает, а не доставляет через #332.
    """
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    monkeypatch.setattr(
        plugins.git_ops, "pr_state", AsyncMock(return_value="closed"), raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops, "pr_for_branch", AsyncMock(return_value=332), raising=False
    )
    task_id = await _approved_task(
        client, db, title="undelivered base calls a human", pr=325
    )
    await repo.update_task(
        db, task_id, branch="task-1204/undelivered-base-calls-a-human"
    )
    await db.commit()

    resp = await _decide_deliver(client, task_id)

    assert resp.status_code == 200, resp.text
    assert spy.merged == [332], "доставлен живой PR, а не закрытый записанный"
    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 332, "живой PR ветки записан на задачу"
    alerts = " ".join(await _alerts(db, task_id))
    assert "#325" in alerts and "#332" in alerts, "оба номера названы в ленте"
    assert "merge_failed" not in alerts


@pytest.mark.parametrize("via", ["poller", "human"])
async def test_a_closed_pr_without_a_replacement_is_named_not_merged(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, via: str
) -> None:
    """AC-3. Записанный PR закрыт, у ветки нет открытого PR ни по одному пути.

    К провайдеру за мержем не обращаются (нет попытки мержа закрытого
    номера), причина названа словами, а не merge_failed, и задача не
    остаётся в молчаливом ожидании — она уходит к человеку.
    """
    branch = "task-1204/undelivered-base-calls-a-human"
    if via == "poller":
        g = _git_with_state("closed", found=None)
        task_id = await _approved_pair_task(db, pr_number=325)
        await repo.update_task(db, task_id, branch=branch)
        await db.commit()

        await _drain_pair_delivery(db)

        g.merge_pr.assert_not_awaited()
    else:
        spy = _MergeSpy()
        _install(monkeypatch, spy)
        monkeypatch.setattr(
            plugins.git_ops,
            "pr_state",
            AsyncMock(return_value="closed"),
            raising=False,
        )
        monkeypatch.setattr(
            plugins.git_ops,
            "pr_for_branch",
            AsyncMock(return_value=None),
            raising=False,
        )
        task_id = await _approved_task(client, db, title="no replacement", pr=325)
        await repo.update_task(db, task_id, branch=branch)
        await db.commit()

        await _decide_deliver(client, task_id)

        assert spy.merged == [], "закрытый PR мержить не пытаются"

    task = dict(await repo.get_task(db, task_id))
    if via == "poller":
        # Свип не может решать за человека: менять номер не на что, и
        # задача уходит в needs_decision, а не остаётся тихо ждать.
        assert task["status"] == "needs_decision", (
            "менять номер не на что — решение человека, а не тихое ожидание"
        )
    else:
        # Человек уже принял решение (accept) — оно не отменяется отказом
        # доставки (#1037): задача остаётся completed, а несостоявшаяся
        # доставка названа в ленте, а не спрятана за успешным статусом.
        assert task["status"] == "completed"
    if via == "poller":
        feed = " ".join(
            dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
        )
    else:
        feed = " ".join(await _alerts(db, task_id))
    assert "325" in feed and "закрыт" in feed
    assert "merge_failed" not in feed, (
        "причина названа явно, а не отказом GitHub по несуществующему PR"
    )


@pytest.mark.parametrize("state", ["merged", "open", ""])
async def test_a_merged_open_or_unknown_recorded_pr_is_never_replaced(
    db: aiosqlite.Connection, state: str
) -> None:
    """AC-4, путь свипа. Смерженный — никогда не заменяется (#605): второй
    мерж не страховка. Открытый и unknown тоже стоят на месте — они и так
    живы, либо про них нечего сказать наверняка (#959)."""
    g = _git_with_state(state, found=999)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 360, f"{state!r}: номер остаётся на месте"
    if state == "merged":
        (
            g.pr_for_branch.assert_not_awaited(),
            ("мерж-состояние не ищет замену — искать нечего"),
        )
    if state:
        assert g.merge_pr.await_args.args[0] == 360, (
            "мержится записанный, второй мерж не заводится"
        )


async def test_a_human_deliver_decision_never_replaces_a_merged_pr(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-4, путь человека: то же самое правило по третьему пути к гейту."""
    spy = _MergeSpy()
    _install(monkeypatch, spy)
    monkeypatch.setattr(
        plugins.git_ops, "pr_state", AsyncMock(return_value="merged"), raising=False
    )
    pr_for_branch = AsyncMock(return_value=999)
    monkeypatch.setattr(plugins.git_ops, "pr_for_branch", pr_for_branch, raising=False)
    task_id = await _approved_task(client, db, title="merged stays merged", pr=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _decide_deliver(client, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 360, "смерженный записанный номер не заменяется"
    pr_for_branch.assert_not_awaited(), "мерж-состояние не ищет замену"


# ---------------------------------------------------------------------------
# AC-5. Сторож полноты: КАЖДЫЙ вызывающий merge_before_completion в hub/
# обязан разрешить PR через #959 (pr_for_delivery / resolve_delivery_pr)
# раньше, чем позвать гейт — сам, либо через кого-то выше по цепочке
# вызовов, кто уже это сделал (так устроен путь отчёта агента:
# _deliver_completed_pair_task получает уже разрешённый delivery_pr от
# _complete_without_review, а не зовёт pr_for_delivery второй раз).
#
# Разбор — по исходнику hub/, а не по примеру: считает вызовы merge_before_
# completion и pr_for_delivery/resolve_delivery_pr как AST Call-узлы (имя
# функции — Name или Attribute.attr, что покрывает и обычный вызов, и
# services.merge_before_completion(...), и локальный import внутри функции),
# строит граф "кто кого зовёт" по именам функций в hub/ и идёт вверх от
# каждого вызывающего гейта, пока не найдёт разрешающий вызов. Новый
# вызывающий мимо правила — без разрешающего вызова нигде в цепочке — роняет
# тест по имени функции, а не стареет молча.
# ---------------------------------------------------------------------------

_MERGE_GATE_FUNC = "merge_before_completion"
_RESOLVER_FUNCS = {"pr_for_delivery", "resolve_delivery_pr"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _called_names(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name:
                names.add(name)
    return names


def _collect_hub_functions(
    hub_dir: Path,
) -> dict[tuple[str, str, int], set[str]]:
    """(файл, имя функции, строка) -> имена всего, что она вызывает."""
    functions: dict[tuple[str, str, int], set[str]] = {}
    for path in sorted(hub_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (str(path), node.name, node.lineno)
                functions[key] = _called_names(node)
    return functions


def test_every_merge_gate_caller_resolves_the_pr_first() -> None:
    hub_dir = Path(__file__).resolve().parents[1] / "hub"
    functions = _collect_hub_functions(hub_dir)

    mergers = {key for key, calls in functions.items() if _MERGE_GATE_FUNC in calls}
    assert mergers, (
        "merge_before_completion не вызывается нигде в hub/ — разбор сломан, "
        "а не то, что вызывающих нет"
    )
    # Живой список сегодняшних вызывающих (#1261) — падение здесь значит
    # появился четвёртый путь к гейту, и его тоже нужно обвести правилом.
    caller_names = {key[1] for key in mergers}
    assert caller_names == {
        "_deliver_completed_pair_task",
        "_deliver_pair_task",
        "deliver_on_disposition",
    }, f"список вызывающих merge_before_completion изменился: {caller_names}"

    resolvers = {key for key, calls in functions.items() if calls & _RESOLVER_FUNCS}

    callers_by_name: dict[str, list[tuple[str, str, int]]] = {}
    for key, calls in functions.items():
        for called in calls:
            callers_by_name.setdefault(called, []).append(key)

    unresolved = []
    for merger in mergers:
        seen: set[tuple[str, str, int]] = set()
        queue = [merger]
        covered = False
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in resolvers:
                covered = True
                break
            queue.extend(callers_by_name.get(current[1], []))
        if not covered:
            unresolved.append(merger[1])

    assert not unresolved, (
        "вызывающие merge_before_completion, для которых ни сам вызывающий, "
        "ни кто-либо выше по цепочке вызовов не зовёт pr_for_delivery/"
        f"resolve_delivery_pr: {sorted(unresolved)}"
    )


# ---------------------------------------------------------------------------
# #1261, ревью сдачи №1 (d464d356): два подтверждённых находки об одной строке
# — безусловной `if delivery_pr.reason: add_task_update(..., "alert", ...)`
# в poller._deliver_pair_task.
#
# [F1, Codex, подтверждено стюардом пробами] Незакоммиченная запись держит
# блокировку SQLite. Когда pr_state — "" (unknown) и merge_before_completion
# на каждый проход отвечает одним и тем же транзиентным отказом (например,
# CI ещё идёт), проход №1 пишет resolver-алерт и _note_pair_delivery_wait
# коммитит. Проход №2 пишет resolver-алерт ЕЩЁ РАЗ, а _note_pair_delivery_wait
# дедуплицирует и возвращается БЕЗ commit — соединение поллера остаётся в
# открытой транзакции. Проба: после прохода №1 db.in_transaction=False, после
# прохода №2 db.in_transaction=True, а вторая коннекция к тому же файлу
# получает OperationalError: database is locked.
#
# [F2, стюард, подтверждено] Алерт повторяется на каждый 30-секундный проход.
# Тот же сетап, 3 прохода дают 3 одинаковых алерта "состояние PR #360
# неизвестно — доставка идёт по нему как раньше" и только 1 запись "Доставка
# отложена". Это ломает #534 ("сказано один раз, а не на каждый цикл"),
# которую сама функция и цитирует.
#
# ЧИНИТСЯ ОДНИМ ходом: resolver-note больше не пишется отдельной, безусловной
# строкой. На транзиентной ветке (единственной, где _deliver_pair_task
# перевызывается на ОДНОЙ и той же задаче раз за разом, пока статус остаётся
# running) она сворачивается в СУЩЕСТВУЮЩИЙ дедуп-и-commit цикл
# _note_pair_delivery_wait/_pair_delivery_waits — второго дедупа не заводится.
# На терминальных ветках (needs_decision из другого отказа, успешная
# доставка) resolver-note пишется как и раньше, безусловно: та задача покидает
# список кандидатов свипа в тот же проход, так что запись не повторяется, а
# существующий commit чуть ниже её уже покрывает.
# ---------------------------------------------------------------------------


async def test_the_resolver_note_is_said_once_across_repeated_transient_waits(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC F2. Три прохода подряд с unknown-состоянием и транзиентным отказом
    гейта — причина резолвера обязана прозвучать РОВНО один раз.

    Проба — как у стюарда: _git_with_state("", found=None),
    _approved_pair_task(pr_number=360), merge_before_completion
    замокан на постоянный transient-отказ, poller._deliver_pair_task зовётся
    3 раза НАПРЯМУЮ (не через свип) с одной и той же задачей.

    На неисправленной сдаче №1 (d464d356) тест красный: resolver-алерт
    пишется безусловно на каждый проход — 3 совпадения вместо 1.
    """
    from hub import poller
    from hub import services as hub_services

    _git_with_state("", found=None)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    transient_detail = hub_services.TRANSIENT_GATE_PREFIXES[0] + " CI ещё идёт"

    async def _stuck_ci(db_, task_):
        return False, transient_detail

    monkeypatch.setattr(hub_services, "merge_before_completion", _stuck_ci)

    for _ in range(3):
        task = dict(await repo.get_task(db, task_id))
        await poller._deliver_pair_task(db, task)

    updates = [
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    ]
    resolver_mentions = [c for c in updates if "PR #360 неизвестно" in c]
    assert len(resolver_mentions) == 1, (
        f"причина резолвера обязана прозвучать один раз, а не на каждый "
        f"проход (#534): {updates}"
    )
    waits = [c for c in updates if "Доставка отложена" in c]
    assert len(waits) == 1, f"и нота ожидания — тоже один раз: {updates}"


async def test_a_deduplicated_wait_pass_leaves_no_open_transaction(
    db: aiosqlite.Connection, db_dsn: str, monkeypatch
) -> None:
    """AC F1. Тот же сетап: дедуплицированный проход (второй с тем же
    транзиентным отказом) не должен оставлять соединение поллера в открытой
    транзакции — ни ``db.in_transaction``, ни блокировкой для второй
    коннекции к тому же файлу.

    На неисправленной сдаче №1 (d464d356) тест красный: после прохода №2
    ``db.in_transaction`` истинно, и INSERT со второй коннекции падает с
    ``OperationalError: database is locked``.
    """
    from hub import poller
    from hub import services as hub_services

    _git_with_state("", found=None)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    transient_detail = hub_services.TRANSIENT_GATE_PREFIXES[0] + " CI ещё идёт"

    async def _stuck_ci(db_, task_):
        return False, transient_detail

    monkeypatch.setattr(hub_services, "merge_before_completion", _stuck_ci)

    task = dict(await repo.get_task(db, task_id))
    await poller._deliver_pair_task(db, task)  # проход №1: не дедуплицирован
    task = dict(await repo.get_task(db, task_id))
    await poller._deliver_pair_task(db, task)  # проход №2: дедуплицирован

    assert db.in_transaction is False, (
        "дедуплицированный проход не должен оставлять соединение поллера "
        "в открытой транзакции"
    )

    second = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await second.execute("PRAGMA busy_timeout = 200")
        await second.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) "
            "VALUES (?, 'probe', 'status', 'second connection probe')",
            (task_id,),
        )
        await second.commit()
    finally:
        await second.close()


# ---------------------------------------------------------------------------
# #1261, стюард — ревью незакоммиченного WIP выше (два теста над этой
# секцией). Тот же F1/F2, но на ВТОРОЙ ветке ожидания: RECOVERABLE_GATE_
# PREFIXES (красный CI) с бюджетом, ещё не потраченным, и исполнителем на
# связи (pair_executor_online -> True) — она тоже зовёт _note_pair_delivery_
# wait и тоже возвращается, до этого WIP-фикса — БЕЗ resolver_note. Ранняя
# версия правки писала delivery_pr.reason безусловно ПЕРЕД проверкой этой
# ветки, думая, что всё после транзиентной ветки терминально; это неверно —
# recoverable-ветка тоже возвращается раньше итогового commit. Те же два
# симптома вернулись здесь: алерт на каждый проход (F2) и открытая
# транзакция после дедуплицированного прохода (F1).
# ---------------------------------------------------------------------------


async def test_the_resolver_note_is_said_once_across_repeated_recoverable_waits(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC F2 (recoverable-ветка). Три прохода подряд: unknown PR-состояние +
    красный CI + исполнитель на связи + бюджет ещё не потрачен —
    RECOVERABLE_GATE_PREFIXES-ветка (#1030), не транзиентная. Причина
    резолвера обязана прозвучать РОВНО один раз здесь тоже.

    На WIP ДО этого фикса (после первого фикса F1/F2, но до переноса
    resolver_note в recoverable-ветку) тест красный: 3 совпадения вместо 1.
    """
    from hub import poller
    from tests.test_pair_merge_gate import _live_session

    g = _git_with_state("", found=None)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.failed, "checks_failed")
    )
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()
    await _live_session(db, task_id)

    for _ in range(3):
        task = dict(await repo.get_task(db, task_id))
        await poller._deliver_pair_task(db, task)

    updates = [
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    ]
    resolver_mentions = [c for c in updates if "PR #360 неизвестно" in c]
    assert len(resolver_mentions) == 1, (
        f"причина резолвера обязана прозвучать один раз и на recoverable-"
        f"ветке (#534): {updates}"
    )
    waits = [c for c in updates if "Доставка отложена" in c]
    assert len(waits) == 1, f"и нота ожидания — тоже один раз: {updates}"
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "исполнитель на связи — это ожидание, не решение"
    )


async def test_a_deduplicated_recoverable_wait_pass_leaves_no_open_transaction(
    db: aiosqlite.Connection, db_dsn: str, monkeypatch
) -> None:
    """AC F1 (recoverable-ветка). Дедуплицированный проход по RECOVERABLE_
    GATE_PREFIXES не должен оставлять соединение поллера в открытой
    транзакции — тот же F1, другая ветка ожидания.

    На WIP ДО этого фикса тест красный: после прохода №2
    ``db.in_transaction`` истинно, и INSERT со второй коннекции падает с
    ``OperationalError: database is locked``.
    """
    from hub import poller
    from tests.test_pair_merge_gate import _live_session

    g = _git_with_state("", found=None)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.failed, "checks_failed")
    )
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()
    await _live_session(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    await poller._deliver_pair_task(db, task)  # проход №1: не дедуплицирован
    task = dict(await repo.get_task(db, task_id))
    await poller._deliver_pair_task(db, task)  # проход №2: дедуплицирован

    assert db.in_transaction is False, (
        "дедуплицированный проход (recoverable-ветка) не должен оставлять "
        "соединение поллера в открытой транзакции"
    )

    second = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await second.execute("PRAGMA busy_timeout = 200")
        await second.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) "
            "VALUES (?, 'probe', 'status', 'second connection probe')",
            (task_id,),
        )
        await second.commit()
    finally:
        await second.close()


async def test_a_human_deliver_decision_commits_the_resolver_note_when_unusable(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#1261: deliver_on_disposition — одноразовый вызов на одно решение
    человека, так что повтор (#534) тут не вопрос, но commit на КАЖДОМ
    исходе, включая unusable, обязан покрывать и resolver-note. Уже верно
    сегодня (единственный commit в самом конце функции покрывает обе ветки);
    тест закрепляет это, чтобы будущая правка с ранним return не открыла
    ту же дыру, что и в поллере.

    #1261 P2 (Codex, подтверждено стюардом на сдаче №2, 8ea2eeac): отказ на
    unusable-исходе писал общий текст "PR остался открытым" — ложь именно
    здесь: записанный PR закрыт/отсутствует, замены нет, мержить нечего, и
    закрытый PR не переоткрывается (docs/agent-context/invariants.md ~57-68).
    Человек пошёл бы искать открытый PR, которого не существует. На сдаче
    №2 (8ea2eeac) тест красный: "остался открытым" есть в ленте задачи."""
    from hub.services import lifecycle as lifecycle_mod

    monkeypatch.setattr(
        plugins.git_ops, "pr_state", AsyncMock(return_value="closed"), raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops, "pr_for_branch", AsyncMock(return_value=None), raising=False
    )
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    ok, reason, search_unanswered = await lifecycle_mod.deliver_on_disposition(
        db, task_id, "deliver", via="test"
    )

    assert ok is False, "закрытый без замены не доставляется"
    assert search_unanswered is False, (
        "поиск здесь ОТВЕТИЛ «нет открытого PR» — это не молчание (#1267)"
    )
    assert db.in_transaction is False, (
        "решение человека не должно оставлять соединение в открытой транзакции"
    )
    updates = [
        (dict(u)["content"] or "") for u in await repo.get_task_updates(db, task_id)
    ]
    feed = " ".join(updates)
    assert "остался открытым" not in feed and "open" not in feed.lower(), (
        f"записанный PR закрыт/отсутствует — открытого PR искать негде: {updates}"
    )
    assert "закрыт" in feed, (
        f"отказ обязан назвать закрытое/отсутствующее состояние: {updates}"
    )


# ---------------------------------------------------------------------------
# #1261, Cursor/grok-4.6 (отчёт #376, находка 40adcc8f02f26c98), подтверждено
# стюардом чтением pr_for_delivery. unusable сам по себе не различает ДВЕ
# разные причины "заменить нечем": поиск живого PR по ветке ОТВЕТИЛ "нет
# такого" (факт, случай AC-3) или поиск УПАЛ с исключением (молчание —
# #725/#802/#959 запрещают читать его как отрицательный факт). Текст
# P2-чинки выше ("у ветки нет открытого PR") был написан только для первого
# случая; для второго это превращает "спросить не удалось" в "замены нет".
# Та же путаница била и по свипу: unusable считался терминальным всегда,
# так что сетевой сбой поиска уводил задачу к человеку вместо повтора на
# следующем проходе.
#
# DeliveryPR получил search_unanswered: True только когда сам поиск упал
# (find_note непусто — единственный путь, которым _live_pr_for_branch его
# заполняет). pr_for_delivery остаётся единственным местом, которое решает.
# ---------------------------------------------------------------------------


def _git_with_failed_replacement_search(state: str = "closed"):
    """Записанный PR в состоянии ``state``, а поиск замены по ветке падает —
    случай (b): "спросить не удалось", а не "открытого PR нет" (случай a)."""
    g = _git_with_state(state, found=None)
    g.pr_for_branch = AsyncMock(side_effect=RuntimeError("gh: rate limited"))
    return g


async def test_a_human_deliver_decision_does_not_claim_no_open_pr_when_the_search_failed(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Тест 1. Записанный PR закрыт, поиск живой замены по ветке УПАЛ.

    Ни одна запись в ленте не должна утверждать «нет открытого PR» — это
    неизвестно, а не установлено. Текст обязан называть, что поиск не
    ответил. На сдаче №3 (dceeec82) тест красный: P2-чинка отвечает "Мержить
    нечего: у ветки нет открытого PR" и для этого случая тоже.

    #1267: после этой задачи выход из молчания появился — реестр
    недоставленного теперь ВИДИТ эту строку (unknown, а не pr_closed), и
    текст обязан назвать это место, а не только повторить, что доставки не
    будет. ``deliver_on_disposition`` возвращает третьим значением
    ``search_unanswered``, которым и передаётся этот факт дальше в
    ``note_completion_without_delivery`` — без второго вычисления состояния.
    """
    from hub.services import lifecycle as lifecycle_mod

    _git_with_failed_replacement_search("closed")
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    ok, reason, search_unanswered = await lifecycle_mod.deliver_on_disposition(
        db, task_id, "deliver", via="test"
    )

    assert ok is False, "закрытый без установленной замены не доставляется"
    assert search_unanswered is True, (
        "поиск замены упал — вызывающий обязан узнать об этом факте (#1267)"
    )
    updates = [
        (dict(u)["content"] or "") for u in await repo.get_task_updates(db, task_id)
    ]
    feed = " ".join(updates)
    assert "нет открытого PR" not in feed, (
        f"поиск не ответил — это не факт об отсутствии открытого PR: {updates}"
    )
    assert "не ответил" in feed, (
        f"отказ обязан назвать, что поиск замены не ответил: {updates}"
    )
    # Cursor #378 (5a41a733cbb3e7be) и #379 (f43a94d860cfc4e1): decide уже
    # записал completed до этой доставки, повторное решение хаб отвергнет.
    # Ни один из этих выходов не существует — текст не вправе их называть.
    assert "принять снова" not in feed, (
        f"повторного решения по завершённой задаче не будет: {updates}"
    )
    # #1267 (было #1261's assertion — "реестр" not in feed): до #1267 реестр
    # недоставленного закрытый-но-неотвеченный PR действительно не показывал,
    # и текст был прав, ничего не называя. После #1267 note_completion_
    # without_delivery пишет эту строку как unknown, реестр её видит и
    # переспросит сам — и текст обязан назвать реестр как место, где работа
    # осталась видимой, потому что это стало правдой (см. AC-4, #1267).
    assert "реестр" in feed, (
        f"после #1267 строка видна в реестре недоставленного — отказ обязан "
        f"назвать его: {updates}"
    )
    assert "БЕЗ доставки" in feed, (
        f"отказ обязан сказать, что задача завершена без доставки: {updates}"
    )


async def test_a_failed_replacement_search_waits_instead_of_calling_a_human(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Тест 2. Тот же сетап через свип: сетевой сбой поиска — не решение
    человека, а повод попробовать на следующем проходе. Три прохода дают
    ровно одну ноту ожидания, задача остаётся running, транзакция закрыта.

    На сдаче №3 (dceeec82) тест красный: свип считает unusable терминальным
    всегда и уводит задачу в needs_decision на первом же проходе.
    """
    from hub import poller

    g = _git_with_failed_replacement_search("closed")
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    for _ in range(3):
        task = dict(await repo.get_task(db, task_id))
        await poller._deliver_pair_task(db, task)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        "сбой поиска замены — временное состояние, не решение человека"
    )
    g.merge_pr.assert_not_awaited()
    updates = [
        (dict(u)["content"] or "") for u in await repo.get_task_updates(db, task_id)
    ]
    waits = [c for c in updates if "Доставка отложена" in c]
    assert len(waits) == 1, (
        f"нота ожидания — один раз на три прохода, не на каждый: {updates}"
    )
    assert db.in_transaction is False, (
        "проход по этой ветке не должен оставлять соединение в открытой транзакции"
    )


# ---- #1271: разбиение перечней, а не копия перечня ----
#
# Тест выше (test_every_status_that_owns_an_unmerged_branch_is_a_base) сверяет
# STACK_DELIVERY_STATUSES с копией, выписанной руками: он ловит удаление члена,
# но не заставляет решать про статус, о котором автор не подумал (#1186:
# pending_report, его повтор, needs_info). Здесь перебирается TaskStatus
# целиком: у каждого члена должно быть решение — либо он в перечне, либо
# исключён с названной причиной, либо открыт находкой со ссылкой на драфт.

# Статус исключён, и вот почему. Причина — наблюдение о коде, а не мнение.
STACK_EXCLUDED_STATUSES: dict[str, str] = {
    TaskStatus.draft.value: (
        "не одобрена к работе: переходы из draft ведут только в open или "
        "rejected, ветки задачи ещё нет, стопке не на чем стоять"
    ),
    TaskStatus.rejected.value: (
        "reject_task отказывает всему, кроме draft — отклонённая задача ветки "
        "не заводила"
    ),
    TaskStatus.completed.value: (
        "принятая без доставки — не ожидание, а вопрос к человеку: обходится "
        "отдельно через list_undelivered_completed_branch_tasks (#1204), "
        "доставленная — стопки не образует"
    ),
}

# Решения нет, и это находка на develop (#1271): статус достижим из running,
# то есть задача в нём может владеть запушенной несмерженной веткой, а обход
# стопки её не видит. Код в этой задаче не правится — драфт ниже.
STACK_UNDECIDED_STATUSES: dict[str, str] = {
    TaskStatus.needs_info.value: "драфт #1275: running → needs_info (hub_ask_question)",
    TaskStatus.open.value: "драфт #1275: running → open (chat_pair_reaper), needs_info → open",
    TaskStatus.claimed.value: "драфт #1275: open → claimed после возврата из running",
    TaskStatus.failed.value: "драфт #1275: running → failed (упавший headless-прогон)",
}


def test_every_task_status_is_either_stacked_or_excluded_with_a_reason() -> None:
    """#1271 AC-1: новый член TaskStatus без решения роняет этот тест.

    Решение — одно из трёх: статус в STACK_DELIVERY_STATUSES, в словаре
    исключённых с причиной, или в словаре открытых находок со ссылкой на
    драфт. Члены берутся из перечисления, а не из головы — ровно та ошибка,
    из-за которой pending_report и needs_info были упущены в #1186.
    """
    from hub.services.orchestration import STACK_DELIVERY_STATUSES

    stacked = set(STACK_DELIVERY_STATUSES)
    buckets = {
        "STACK_DELIVERY_STATUSES": stacked,
        "STACK_EXCLUDED_STATUSES": set(STACK_EXCLUDED_STATUSES),
        "STACK_UNDECIDED_STATUSES": set(STACK_UNDECIDED_STATUSES),
    }
    members = {status.value for status in TaskStatus}

    undecided = sorted(members - set().union(*buckets.values()))
    assert not undecided, (
        f"статус без решения: {undecided} — внесите его в "
        "STACK_DELIVERY_STATUSES (может владеть несмерженной веткой) или в "
        "STACK_EXCLUDED_STATUSES с причиной"
    )
    for name, bucket in buckets.items():
        stale = sorted(bucket - members)
        assert not stale, f"{name} называет статусы, которых нет в TaskStatus: {stale}"
    names = list(buckets)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            both = sorted(buckets[left] & buckets[right])
            assert not both, f"статус решён дважды ({left} и {right}): {both}"
    for status, reason in {
        **STACK_EXCLUDED_STATUSES,
        **STACK_UNDECIDED_STATUSES,
    }.items():
        assert reason.strip(), f"исключение {status} без причины"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#1271 находка на develop, драфт #1275: статусы, достижимые "
    "из running, не входят в STACK_DELIVERY_STATUSES и решения не имеют",
)
def test_undecided_stack_statuses_are_decided() -> None:
    # Проверяет код, а не словарь: статус, достижимый из running, входит в
    # обход стопки. Когда драфт это решит, strict уронит XPASS, а основной
    # тест — «решён дважды», и словарь находок придётся убрать.
    from hub.services.orchestration import STACK_DELIVERY_STATUSES

    missing = sorted(set(STACK_UNDECIDED_STATUSES) - set(STACK_DELIVERY_STATUSES))
    assert not missing, f"не решены: {missing}"


# ---- #1271 AC-2: у каждого транзитного префикса своя подсказка ----

# Префиксы, которым общая подсказка положена по смыслу: ключ делит подсказку
# со значением, и сказано почему.
TRANSIENT_SHARED_HINTS: dict[str, tuple[str, str]] = {
    STACK_UNKNOWN_PREFIX: (
        STACKED_BASE_PREFIX,
        "повторяемый unknown стопки ждёт того же, что стопка: следующего цикла, "
        "а не пересдачи; CI уже зелёный (#1186)",
    ),
}

# Префиксы, чья подсказка унаследована от чужой ветки, — находка на develop.
TRANSIENT_HINT_FINDINGS: dict[str, str] = {
    MERGE_UNCONFIRMED: (
        "драфт #1276: мерж состоялся, CI уже зелёный, а лесенка "
        "done-flow говорит «отчитайтесь снова, когда CI станет зелёным»"
    ),
}


async def _transient_hint(db: aiosqlite.Connection, monkeypatch, prefix: str) -> str:
    """Подсказка, которую лесенка done-flow даёт отказу с этим префиксом."""
    from hub.services import orchestration

    task_id = await _approved_pair_task(db, pr_number=4242)
    detail = f"{prefix}: проба #1271"

    async def _refuse(db_, task_):
        return False, detail

    monkeypatch.setattr(orchestration, "merge_before_completion", _refuse)
    task = dict(await repo.get_task(db, task_id))
    outcome = await orchestration._deliver_completed_pair_task(
        db, task, orchestration.DeliveryPR(number=4242)
    )
    assert outcome == "running", f"{prefix}: транзитный отказ обязан оставить running"
    lead = f"Доставка отложена: PR #4242 — {detail}. "
    alerts = [
        dict(u)["content"] or ""
        for u in await repo.get_task_updates(db, task_id)
        if (dict(u)["content"] or "").startswith(lead)
    ]
    assert len(alerts) == 1, f"{prefix}: ожидалась одна нота ожидания, есть {alerts}"
    return alerts[0][len(lead) :]


async def test_every_transient_gate_prefix_has_its_own_hint(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """#1271 AC-2: префикс, добавленный без своей ветки подсказки, роняет тест.

    #1186 ×2: STACKED_BASE и STACK_UNKNOWN попали в кортеж, а лесенка их не
    знала — и они унаследовали фразу про CI, которая советовала пересдачу,
    сбрасывающую вердикт. Правило: фраза про CI принадлежит префиксам CI
    (ci_<CIProbeOutcome>, выводятся из перечисления), любой другой префикс
    обязан получить иную подсказку, а общая подсказка двух не-CI префиксов
    объявляется в TRANSIENT_SHARED_HINTS с причиной.
    """
    from hub.integrations.protocols import CIProbeOutcome
    from hub.services.orchestration import TRANSIENT_GATE_PREFIXES

    ci_family = {f"ci_{o.value}" for o in CIProbeOutcome}
    hints = {
        prefix: await _transient_hint(db, monkeypatch, prefix)
        for prefix in TRANSIENT_GATE_PREFIXES
    }
    ci_hints = {hints[p] for p in TRANSIENT_GATE_PREFIXES if p in ci_family}
    assert ci_hints, "в кортеже нет ни одного CI-префикса — правило не о чем"

    for key in {*TRANSIENT_SHARED_HINTS, *TRANSIENT_HINT_FINDINGS}:
        assert key in TRANSIENT_GATE_PREFIXES, f"{key!r} уже не в кортеже"

    by_hint: dict[str, list[str]] = {}
    for prefix in TRANSIENT_GATE_PREFIXES:
        if prefix in ci_family or prefix in TRANSIENT_HINT_FINDINGS:
            continue
        assert hints[prefix] not in ci_hints, (
            f"префикс {prefix!r} унаследовал подсказку CI: {hints[prefix]!r} — "
            "дайте ему свою ветку в лесенке _deliver_completed_pair_task"
        )
        by_hint.setdefault(hints[prefix], []).append(prefix)

    for hint, group in by_hint.items():
        for prefix in group[1:]:
            declared = TRANSIENT_SHARED_HINTS.get(prefix, ("", ""))
            assert declared[0] in group and declared[1].strip(), (
                f"префиксы {group} делят подсказку {hint!r}, а общая подсказка "
                f"для {prefix!r} не объявлена в TRANSIENT_SHARED_HINTS с причиной"
            )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#1271 находка на develop, драфт #1276: MERGE_UNCONFIRMED "
    "получает в done-flow фразу про CI",
)
async def test_transient_hint_findings_are_resolved(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    from hub.integrations.protocols import CIProbeOutcome

    ci_hint = await _transient_hint(
        db, monkeypatch, f"ci_{CIProbeOutcome.pending.value}"
    )
    for prefix in TRANSIENT_HINT_FINDINGS:
        assert await _transient_hint(db, monkeypatch, prefix) != ci_hint


async def test_a_base_that_moved_between_probe_and_push_refuses_the_stale_resolution(
    tmp_path,
) -> None:
    """Находка 2327bd9255c601cc (high), воспроизведённая настоящим git.

    Аренда пуша стережёт ВЕТКУ задачи, а дерево для пуша строится заново и
    сливает уже новый ``origin/develop``. Разрешение же посчитано на пробе, то
    есть на старой базе. Раньше его байты просто ложились поверх свежего файла
    и шли в ``git add``: признак U с пути снимался, проверка «остались ли
    конфликты» видела чистое дерево, и строки, приехавшие в базу между пробой
    и пушем, пропадали молча — в мерж-коммите, у которого MERGE_HEAD как раз
    новый develop.

    Здесь третья доставка кладёт в develop ещё один хвост ровно в окне между
    пробой и пушем (изнутри валидации, которая гонится в готовом дереве).
    Автомерж обязан отказать, а не сложить старое разрешение на новое дерево.
    """
    import subprocess

    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")

    files, why = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, pinned
    )
    assert files, why
    resolutions, _ = base_merge.plan_resolution(files)
    assert resolutions
    # ОКНО МЕЖДУ ПРОБОЙ И ПУШЕМ: третья доставка кладёт на develop ещё хвост.
    # Дерево для пуша строится заново и сливает уже ЭТОТ develop, а разрешение
    # посчитано на прежнем.
    _run_git("git", "checkout", "-q", "develop", cwd=repo)
    suite = repo / "tests_suite.py"
    suite.write_text(suite.read_text() + "\n\ndef test_from_the_third():\n    pass\n")
    _run_git("git", "commit", "-q", "-am", "third delivery lands on develop", cwd=repo)
    _run_git("git", "push", "-q", "origin", "develop", cwd=repo)

    async def _green_but_the_base_moves(_path):
        return 0, "ok"

    before = _tip(repo, "task-1233/probe")
    ok, detail = await ops.push_resolved_base_merge(
        str(repo),
        "develop",
        "task-1233/probe",
        1233,
        resolutions,
        _green_but_the_base_moves,
        pinned,
        files,
    )

    assert not ok, (
        "база сдвинулась под разрешением — старые байты на новое дерево класть нельзя"
    )
    assert "база сдвинулась между пробой и пушем" in detail
    assert _tip(repo, "task-1233/probe") == before, (
        "отказавший автомерж не двигает ветку"
    )
    _run_git("git", "fetch", "-q", "origin", cwd=repo)
    on_branch = subprocess.run(
        ["git", "show", "origin/task-1233/probe:tests_suite.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "test_from_the_base" not in on_branch, (
        "ничего не слито: ветка стоит там же, где стояла"
    )


async def test_a_push_without_the_probe_refuses_instead_of_resolving_blind(
    tmp_path,
) -> None:
    """Сверять разрешение не с чем — отказ, а не «ну применим как есть».

    Вторая половина той же находки: без пробы пуш физически не может узнать,
    на том ли дереве считалось разрешение. Раньше этот вход был единственным,
    и именно он и применял старые байты вслепую.
    """
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.services import base_merge

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")
    files, _ = await ops.base_merge_conflicts(
        str(repo), "develop", "task-1233/probe", 1233, pinned
    )
    resolutions, _ = base_merge.plan_resolution(files or {})

    async def _green(_path):
        return 0, "ok"

    before = _tip(repo, "task-1233/probe")
    ok, detail = await ops.push_resolved_base_merge(
        str(repo), "develop", "task-1233/probe", 1233, resolutions, _green, pinned
    )

    assert not ok and "не с чем сверить" in detail
    assert _tip(repo, "task-1233/probe") == before


async def test_a_cancelled_base_merge_leaves_no_scratch_worktree(
    tmp_path, monkeypatch
) -> None:
    """Отмена посреди мержа базы не бросает одноразовое дерево (#1233).

    Находка f529be6df1e61160. Между ``worktree add`` и возвратом пути мерж
    базы шёл без страховки: ``CancelledError`` (гашение поллера, отмена
    вызова) пролетал мимо, а вызывающие чистят дерево только когда путь им
    вернули. Дерево оставалось рядом с клоном с незавершённым мержем внутри,
    и убиралось лишь следующей попыткой той же задачи — если она будет.
    """
    import asyncio
    import os

    from hub.integrations import git_ops as git_ops_mod
    from hub.integrations.git_ops import GitOpsIntegration

    repo = _repo_with_a_tail_conflict(tmp_path)
    ops = GitOpsIntegration()
    pinned = _tip(repo, "task-1233/probe")
    real_git = git_ops_mod._git

    async def _cancelled_merge(*args, **kw):
        if "merge" in args and "--no-commit" in args:
            raise asyncio.CancelledError
        return await real_git(*args, **kw)

    monkeypatch.setattr(git_ops_mod, "_git", _cancelled_merge)

    with pytest.raises(asyncio.CancelledError):
        await ops.base_merge_conflicts(
            str(repo), "develop", "task-1233/probe", 1233, pinned
        )

    scratch = git_ops_mod._scratch_worktree(str(repo), "basemerge", 1233)
    assert not os.path.exists(scratch), "отменённый мерж не оставляет дерево"
    listed = await real_git("worktree", "list", "--porcelain", repo=str(repo))
    assert scratch not in (listed[1] or ""), "и git о нём тоже не помнит"
