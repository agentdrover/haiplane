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
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.services import validation_run
from hub.services.delivery_gate import undelivered_warning
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
        str(repo), "develop", "task-1233/probe", 1233, resolutions, _red, pinned
    )
    assert not ok and "код возврата 2" in detail, (
        "хвост вывода не доказательство — судим по коду возврата"
    )
    assert _tip(repo, "task-1233/probe") == before, "красная валидация не пушит ничего"

    async def _green(_path):
        return 0, "ok"

    ok, sha = await ops.push_resolved_base_merge(
        str(repo), "develop", "task-1233/probe", 1233, resolutions, _green, pinned
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

    async def _push(repo, base, branch, task_id, resolutions, validate=None, tip=""):
        seen["rc"], seen["log"] = await validate("/tmp/basemerge")
        seen["tip"] = tip
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
        str(repo), "develop", "task-1233/probe", 1233, resolutions, _green, approved
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
