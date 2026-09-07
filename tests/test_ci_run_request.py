"""Один запрос прогона, когда прогонов по коммиту нет вовсе (#1197).

Механизм узкий по замыслу, и почти весь этот файл — про его ГРАНИЦЫ, а не
про то, что запрос происходит. Право «попросить прогон» в одном шаге от
«попроси ещё раз, вдруг позеленеет», а это ферма флейков: исход перестаёт
быть свойством кода и становится свойством числа попыток.

Наблюдённый случай, из которого выросла задача, — #1185 06.09.2026: коммит
9ac59bb без единого прогона, APPROVED получен, доставка отложена по
ci_missing_run, задача принята решением человека и осталась вне develop.
Человека позвали ради действия, которое хаб может выполнить сам.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import brand
from hub import repository as repo
from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    CIRunRequestOutcome,
    CIRunRequestResult,
)
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.registry import plugins
from tests.test_pair_merge_gate import _approved_pair_task, _git, _report_done
from tests.test_poller import (
    _BreakLoop,
    _events_for,
    _make_app,
    _make_ci_task,
    _poll_running_tasks,
    _sleep_once,
)

PINNED = "feedfacecafebeeffeedfacecafebeeffeedface"
BRANCH = "task-1197/geit-dostavki-umeet-zhdat-progon-i-eskal"


def _gate_seeing_no_runs(*, request: CIRunRequestResult | None = None):
    """Гейт, у которого probe отвечает missing_run по закреплённому коммиту."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details=PINNED
        )
    )
    g.request_ci_run = AsyncMock(
        return_value=request
        or CIRunRequestResult(CIRunRequestOutcome.requested, "workflow_dispatched")
    )
    plugins.git_ops = g
    return g


async def _pin(db: aiosqlite.Connection, task_id: int, *, sha: str = PINNED) -> None:
    """Закрепить коммит сдачи и ветку — то, что гейт сверяет перед запросом."""
    await db.execute(
        "UPDATE tasks SET submission_sha=?, branch=? WHERE id=?",
        (sha, BRANCH, task_id),
    )
    await db.commit()


async def _age_the_window(db: aiosqlite.Connection, task_id: int) -> None:
    await db.execute(
        "UPDATE tasks SET ci_check_started_at = datetime('now', '-30 minutes') "
        "WHERE id=?",
        (task_id,),
    )
    await db.commit()


# ---- AC-1: коммит без единого прогона получает ОДИН запрос ----


async def test_a_commit_without_any_run_gets_one_request(db: aiosqlite.Connection):
    g = _gate_seeing_no_runs()
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)

    await _report_done(db, task_id)

    assert g.request_ci_run.await_count == 1, (
        "прогонов по этому коммиту нет ни одного — просить законно и нужно"
    )
    assert g.request_ci_run.await_args.args[0] == BRANCH, (
        "dispatch идёт по ref, и ref — это ветка задачи"
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["ci_run_requested_sha"] == PINNED, (
        "факт запроса держится ДАННЫМИ и ключом ему служит сам коммит"
    )
    assert task["ci_check_started_at"], (
        "окно грейса отсчитывается от этой отметки; без неё заказанному "
        "прогону не отводится времени появиться"
    )
    assert task["status"] == "running", "запрос не доставляет и не судит"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "Прогон CI запрошен хабом" in body, (
        "факт запроса виден человеку в карточке, а не только в журнале"
    )


# ---- AC-2: проваленный прогон переиграть нельзя, и это по КАЖДОМУ исходу ----


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "startup_failure", "action_required"],
)
async def test_a_failed_run_is_never_replayed(monkeypatch, conclusion: str):
    """Каждый провальный исход даёт failed, а не missing_run.

    Проверяется на самом probe, а не на одном срезе гейта: правило «никогда
    после провала» держится тем, что ветка запроса при существующем прогоне
    НЕДОСТИЖИМА, и это свойство обязано быть верным для каждого исхода
    поимённо. Один общий тест на failed показал бы, что не сломано сегодня, и
    промолчал бы о дне, когда в список забыли добавить исход.
    """

    async def fake_run(*cmd, **kw):
        argv = " ".join(cmd)
        if "pr checks" in argv:
            return (1, "", "checks:read forbidden")
        if "headRefOid" in argv:
            return (0, '{"headRefOid": "' + PINNED + '"}', "")
        if "actions/runs" in argv:
            payload = (
                '{"workflow_runs": [{"status": "completed", "conclusion": "'
                + conclusion
                + '"}]}'
            )
            return (0, payload, "")
        raise AssertionError(f"probe went somewhere unexpected: {argv}")

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)

    probe = await GitHubForge().check_pr_ci(7, gh_repo="own/rep")

    assert probe.outcome is CIProbeOutcome.failed
    assert probe.outcome is not CIProbeOutcome.missing_run, (
        f"{conclusion} — исход уже сказан; missing_run открыл бы дорогу запросу"
    )


async def test_the_gate_asks_for_nothing_when_a_run_already_spoke(
    db: aiosqlite.Connection,
):
    """Вторая половина AC-2: при failed гейт не просит ничего.

    Проверка «probe был вызван» стоит здесь не для красоты: без неё тест
    зелен и в том случае, когда гейт до чтения CI вообще не дошёл, — то
    есть стерёг бы правило, которое ни разу не проверял.
    """
    g = _gate_seeing_no_runs()
    # details НЕ пуст намеренно: с пустым коммитом запрос отсекала бы сверка
    # вершины, и тест был бы зелен по чужой причине. Первая редакция этого
    # теста ошибалась ровно так — правило «не после провала» переживало
    # мутацию, которая звала запрос прямо из ветки failed.
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.failed, "workflow_runs_failed", details=PINNED
        )
    )
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)

    await _report_done(db, task_id)

    assert g.check_pr_ci.await_count >= 1, "гейт не дошёл до чтения CI"
    g.request_ci_run.assert_not_awaited()


# ---- AC-3: одна попытка на коммит, дальше — прежний путь к человеку ----


async def test_one_request_per_sha_then_the_human(db: aiosqlite.Connection):
    g = _gate_seeing_no_runs()
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)

    # Первый круг: запрос сделан, окно перезапущено, человека не зовут.
    await _report_done(db, task_id)
    assert g.request_ci_run.await_count == 1
    assert dict(await repo.get_task(db, task_id))["status"] == "running"

    # Второй круг по ТОМУ ЖЕ коммиту, окно истекло: второго запроса нет,
    # и backstop работает ровно как до правки.
    await _age_the_window(db, task_id)
    await _report_done(db, task_id)

    assert g.request_ci_run.await_count == 1, (
        "вторая попытка сделала бы исход свойством числа попыток"
    )
    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "эскалация к человеку сохранилась целиком: запрос стоит ПЕРЕД "
        "backstop'ом, а не вместо него"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "workflow есть" in body, "прежний ci_untested никуда не делся"


async def test_a_moved_branch_buys_no_run(db: aiosqlite.Connection):
    """Запуск идёт по ref, а исход читается по SHA — расхождение не оплачиваем."""
    g = _gate_seeing_no_runs()
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id, sha="0ther0commit0000000000000000000000000000")

    await _report_done(db, task_id)

    g.request_ci_run.assert_not_awaited()
    task = dict(await repo.get_task(db, task_id))
    assert not task["ci_run_requested_sha"]


# ---- AC-4: неудавшийся вызов не съедает единственную попытку ----


@pytest.mark.parametrize(
    "result",
    [
        CIRunRequestResult(
            CIRunRequestOutcome.declined, "workflow_dispatch_not_declared_on_ref"
        ),
        CIRunRequestResult(CIRunRequestOutcome.unavailable, "dispatch_call_failed"),
        CIRunRequestResult(CIRunRequestOutcome.unsupported, "forge_requests_nothing"),
    ],
)
async def test_a_failed_request_is_not_a_spent_attempt(
    db: aiosqlite.Connection, result: CIRunRequestResult
):
    g = _gate_seeing_no_runs(request=result)
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)

    await _report_done(db, task_id)  # цикл не падает

    task = dict(await repo.get_task(db, task_id))
    assert not task["ci_run_requested_sha"], (
        "потраченной считается только УДАВШАЯСЯ попытка"
    )

    # Следующий круг вправе попробовать снова — попытка не сгорела.
    g.request_ci_run = AsyncMock(
        return_value=CIRunRequestResult(
            CIRunRequestOutcome.requested, "workflow_dispatched"
        )
    )
    await _report_done(db, task_id)

    assert g.request_ci_run.await_count == 1
    assert dict(await repo.get_task(db, task_id))["ci_run_requested_sha"] == PINNED


async def test_a_refused_request_names_its_reason(db: aiosqlite.Connection):
    """Причина названа своими словами там, где человек читает отказ."""
    g = _gate_seeing_no_runs(
        request=CIRunRequestResult(
            CIRunRequestOutcome.declined, "workflow_dispatch_not_declared_on_ref"
        )
    )
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)
    await _report_done(db, task_id)
    await _age_the_window(db, task_id)

    await _report_done(db, task_id)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    body = " ".join(u.get("content") or "" for u in updates)
    assert "workflow_dispatch_not_declared_on_ref" in body
    assert g.request_ci_run.await_count == 2, (
        "отказ форжа лечится не повтором вызова, но и попытку он не съедает"
    )


# ---- AC-5: GitVerse не трогается вовсе, и это держит тест ----


async def test_gitverse_is_left_alone(monkeypatch):
    """Граница объявлена решением владельца 06.09.2026 и проверена поведением.

    Не «мы не собирались его звать», а «позвали и убедились, что наружу он не
    сходил»: подменённый httpx падает от любого запроса.
    """
    import httpx

    class Exploding(httpx.AsyncClient):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            raise AssertionError("GitVerse не должен ходить наружу за прогоном")

    monkeypatch.setattr(httpx, "AsyncClient", Exploding)

    result = await GitVerseForge(
        token="t", base_url="https://api.example", version="1"
    ).request_ci_run("task-1/x", gh_repo="own/rep")

    assert result.outcome is CIRunRequestOutcome.unsupported
    assert result.outcome is not CIRunRequestOutcome.requested


async def test_the_github_request_discovers_its_workflow(monkeypatch):
    """Workflow не назван строкой, а найден — и псевдо-workflow отсеяны.

    GitHub отдаёт в том же списке записи Dependabot с path вида
    dynamic/dependabot/..., которым не соответствует ни один файл. Наблюдено
    на живом репозитории 06.09.2026: без фильтра по .github/workflows/
    кандидатов оказывается три, и «ровно один» никогда не выполняется.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_run(*cmd, **kw):
        calls.append(cmd)
        argv = " ".join(cmd)
        if "actions/workflows" in argv and "dispatches" not in argv:
            return (
                0,
                '{"workflows": ['
                '{"id": 1, "state": "active", "path": ".github/workflows/ci.yml"},'
                '{"id": 2, "state": "active", "path": "dynamic/dependabot/x"},'
                '{"id": 3, "state": "disabled_manually",'
                ' "path": ".github/workflows/old.yml"}]}',
                "",
            )
        return (0, "", "")

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)

    result = await GitHubForge().request_ci_run("task-1/x", gh_repo="own/rep")

    assert result.outcome is CIRunRequestOutcome.requested
    argv = " ".join(calls[-1])
    assert "actions/workflows/1/dispatches" in argv, (
        f"дёрнут должен быть единственный настоящий workflow: {argv}"
    )
    assert "ref=task-1/x" in argv


async def test_a_ref_without_the_trigger_is_declined_not_broken(monkeypatch):
    """422 GitHub — законный ответ «на этом ref триггера нет», а не сбой.

    Текст наблюдён на живом репозитории 06.09.2026: тот же вызов на ветке,
    отрезанной до объявления триггера, отвечает 422, а на develop — рождает
    прогон. Если бы это читалось как unavailable, хаб просил бы снова и снова
    там, где ответ уже дан.
    """

    async def fake_run(*cmd, **kw):
        argv = " ".join(cmd)
        if "dispatches" in argv:
            return (
                1,
                "",
                "HTTP 422: Workflow does not have 'workflow_dispatch' trigger",
            )
        return (
            0,
            '{"workflows": [{"id": 1, "state": "active",'
            ' "path": ".github/workflows/ci.yml"}]}',
            "",
        )

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)

    result = await GitHubForge().request_ci_run("task-1/x", gh_repo="own/rep")

    assert result.outcome is CIRunRequestOutcome.declined
    assert result.outcome is not CIRunRequestOutcome.unavailable


# ---- Тот же механизм на втором вызывающем: поллер ----


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_the_poller_asks_before_it_calls_a_human(mock_sleep, db):
    """Поллер зовёт человека ради кнопки, которую хаб может нажать сам.

    Второй вызывающий той же ветки missing_run — и он обязан вести себя так
    же, иначе правило держалось бы местом вызова, а не данными. Здесь окно
    грейса УЖЕ истекло (иначе поллер не дошёл бы до probe), так что без
    правки этот тик увёл бы задачу в needs_decision.
    """
    task_id = await _make_ci_task(db)
    await _pin(db, task_id)
    mock_dispatch = NoopDispatch()
    mock_dispatch.submit_task = AsyncMock(return_value={"job_id": "rev-1"})
    plugins.dispatch = mock_dispatch
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details=PINNED
        )
    )
    mock_git.request_ci_run = AsyncMock(
        return_value=CIRunRequestResult(
            CIRunRequestOutcome.requested, "workflow_dispatched"
        )
    )
    plugins.git_ops = mock_git

    before = dict(await repo.get_task(db, task_id))["ci_check_started_at"]

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "ci_check", "запрос сделан — человека звать не за чем"
    assert row["ci_run_requested_sha"] == PINNED
    assert mock_git.request_ci_run.await_count == 1
    assert await _events_for(db, task_id, "needs_decision") == []
    # Само по себе «остались в ci_check» ничего не стоит: поллер щупает PR
    # только после грейса, а окно здесь уже состарено на 30 минут. Без
    # перезапуска следующий тик через POLL_INTERVAL увидит потраченную попытку
    # и уведёт задачу к человеку раньше, чем заказанный прогон успеет
    # появиться, — то есть запрос не купит ничего. Проверяем ЧАСЫ, а не
    # статус: без этой строки удаление перезапуска оставляло сюиту зелёной.
    assert row["ci_check_started_at"] != before, (
        "окно грейса обязано начаться заново после удавшегося запроса"
    )


@patch("hub.poller.asyncio.sleep", new_callable=_sleep_once)
async def test_the_poller_still_reaches_the_human_on_the_next_tick(mock_sleep, db):
    """Backstop цел: попытка потрачена — следующий тик эскалирует, как прежде."""
    task_id = await _make_ci_task(db)
    await _pin(db, task_id)
    await db.execute(
        "UPDATE tasks SET ci_run_requested_sha=? WHERE id=?", (PINNED, task_id)
    )
    await db.commit()
    plugins.dispatch = NoopDispatch()
    mock_git = NoopGitOps()
    mock_git.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(
            CIProbeOutcome.missing_run, "no_workflow_runs", details=PINNED
        )
    )
    mock_git.request_ci_run = AsyncMock(
        return_value=CIRunRequestResult(
            CIRunRequestOutcome.requested, "workflow_dispatched"
        )
    )
    plugins.git_ops = mock_git

    with pytest.raises(_BreakLoop):
        await _poll_running_tasks(_make_app(db))

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "needs_decision"
    mock_git.request_ci_run.assert_not_awaited()


async def test_the_gate_restarts_the_window_only_after_a_granted_request(
    db: aiosqlite.Connection,
):
    """Продление окна — не бесплатная добавка, а следствие потраченной попытки.

    Отказ форжа НЕ должен двигать часы: иначе задача с необъявленным триггером
    получала бы свежий грейс на каждом круге и никогда не доходила до человека
    — то есть конечное ожидание стало бы тихим бесконечным циклом.
    """
    _gate_seeing_no_runs(
        request=CIRunRequestResult(
            CIRunRequestOutcome.declined, "workflow_dispatch_not_declared_on_ref"
        )
    )
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)
    await _report_done(db, task_id)
    await _age_the_window(db, task_id)
    aged = dict(await repo.get_task(db, task_id))["ci_check_started_at"]

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["ci_check_started_at"] == aged, (
        "отказ не продлевает ожидание — иначе backstop не наступает никогда"
    )
    assert task["status"] == "needs_decision"


# ---- Заявка ставится ДО платного вызова, а не после ----


async def test_the_attempt_is_reserved_before_the_paid_call(db: aiosqlite.Connection):
    """Два вызывающих на разных соединениях не должны оплатить два прогона.

    Проверяется не «мы вызвали claim», а НАБЛЮДАЕМОЕ следствие: пока запрос в
    полёте, попытка уже занята в базе. Соперник, читающий колонку в этот
    момент, видит занято — а при прежней форме (проверка по снимку, запись
    после await) видел бы пусто и заказал бы второй прогон.
    """
    seen: dict[str, str | None] = {}

    async def slow_request(branch, **kw):
        row = await db.execute_fetchall(
            "SELECT ci_run_requested_sha FROM tasks WHERE submission_sha=?", (PINNED,)
        )
        seen["mid_flight"] = dict(row[0])["ci_run_requested_sha"]
        return CIRunRequestResult(CIRunRequestOutcome.requested, "workflow_dispatched")

    g = _gate_seeing_no_runs()
    g.request_ci_run = AsyncMock(side_effect=slow_request)
    task_id = await _approved_pair_task(db)
    await _pin(db, task_id)

    await _report_done(db, task_id)

    assert seen["mid_flight"] == PINNED, (
        "во время платного вызова попытка обязана быть уже занята"
    )


# ---- Очистка состояния конвейера не возвращает вторую попытку ----


async def test_leaving_the_conveyor_does_not_refund_the_attempt(
    db: aiosqlite.Connection,
):
    """reset_ci_check_state чистит часы и счётчик PR — но НЕ занятую попытку.

    Путь, на котором это стоит денег, живой: поллер эскалирует и чистит
    состояние → человек отправляет в доработку → задача возвращается в
    ci_check с ТЕМ ЖЕ закреплённым коммитом. Вернув попытку, хаб оплатил бы по
    тому же SHA второй прогон, и «одна попытка на коммит» держалась бы только
    на том, что этим путём редко ходят.
    """
    task_id = await _make_ci_task(db)
    await _pin(db, task_id)
    assert await repo.claim_ci_run_request(db, task_id, PINNED)
    await db.commit()

    await repo.reset_ci_check_state(db, task_id)
    await db.commit()

    row = dict(await repo.get_task(db, task_id))
    assert row["ci_check_started_at"] is None, "часы конвейера чистятся, как и раньше"
    assert row["ci_run_requested_sha"] == PINNED, (
        "попытка привязана к коммиту, а не к заходу на конвейер"
    )
    assert not await repo.claim_ci_run_request(db, task_id, PINNED), (
        "второй заявки по тому же коммиту не бывает"
    )


# ---- Поиск workflow: спутник хаба несёт ДВА, и это норма ----


async def test_a_hub_seeded_satellite_still_gets_its_run(monkeypatch):
    """Спутник, выданный хабом, несёт haiplane-ci.yml И haiplane-stale.yml.

    Правило «ровно один кандидат» отказывало бы на КАЖДОМ таком репозитории —
    то есть механизм был бы мёртв ровно там, где комментарий о поиске обещал
    его работу. Дёргается тот workflow, который хаб сам туда положил как CI.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_run(*cmd, **kw):
        calls.append(cmd)
        argv = " ".join(cmd)
        if "actions/workflows" in argv and "dispatches" not in argv:
            return (
                0,
                '{"workflows": ['
                '{"id": 5, "state": "active",'
                f' "path": ".github/workflows/{brand.SEEDED_STALE}"}},'
                '{"id": 9, "state": "active",'
                f' "path": ".github/workflows/{brand.SEEDED_CI}"}}]}}',
                "",
            )
        return (0, "", "")

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)

    result = await GitHubForge().request_ci_run("task-1/x", gh_repo="own/rep")

    assert result.outcome is CIRunRequestOutcome.requested
    assert "actions/workflows/9/dispatches" in " ".join(calls[-1]), (
        "дёрнут должен быть CI, а не подметальщик просроченных"
    )


async def test_an_unknown_workflow_set_is_declined_not_guessed(monkeypatch):
    """Незнакомый набор — отказ по имени, и это НЕ перестраховка.

    Гейт читает ЛЮБОЙ прогон по закреплённому коммиту и пропускает
    success|neutral|skipped. Значит дёрнутый наугад посторонний workflow не
    просто не помогает: он изготавливает зелёный прогон для коммита, чьи тесты
    не выполнялись, и гейт этому верит. Догадка здесь покупает галочку, что
    хуже, чем не купить ничего.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_run(*cmd, **kw):
        calls.append(cmd)
        if "dispatches" in " ".join(cmd):
            raise AssertionError("посторонний workflow дёргать нельзя")
        return (
            0,
            '{"workflows": [{"id": 3, "state": "active",'
            ' "path": ".github/workflows/release-please.yml"}]}',
            "",
        )

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)

    result = await GitHubForge().request_ci_run("task-1/x", gh_repo="own/rep")

    assert result.outcome is CIRunRequestOutcome.declined
    assert result.reason == "no_known_ci_workflow"
    assert "release-please.yml" in (result.details or ""), (
        "отказ обязан назвать, что именно он видел"
    )
