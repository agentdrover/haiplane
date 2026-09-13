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
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services.delivery_gate import undelivered_warning
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


def _probes(g, outcome, reason: str = "scripted"):
    """Script the stacking probe on a git double (#1186)."""
    from hub.integrations.protocols import StackProbeResult

    g.branch_stacking_probe = AsyncMock(
        return_value=StackProbeResult(outcome=outcome, reason=reason)
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
    ту же дыру, что и в поллере."""
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

    ok, reason = await lifecycle_mod.deliver_on_disposition(
        db, task_id, "deliver", via="test"
    )

    assert ok is False, "закрытый без замены не доставляется"
    assert db.in_transaction is False, (
        "решение человека не должно оставлять соединение в открытой транзакции"
    )
