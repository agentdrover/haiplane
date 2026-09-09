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

from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services.delivery_gate import undelivered_warning
from hub.services.delivery_state import (
    DELIVERED,
    PR_CLOSED,
    PR_OPEN,
    UNKNOWN,
)
from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done


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
    # Второе состояние ТОГО ЖЕ словаря: PR закрыт без мержа. Работу свернули
    # намеренно — это тоже не «ждём доставки», и в кандидаты оно не входит.
    # Закрепляется здесь, потому что allowlist из одного токена читается как
    # опечатка ровно до тех пор, пока рядом не названо, что ещё бывает.
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
    assert closed not in ids, "закрытый без мержа PR — тоже не «ждём доставки»"


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
