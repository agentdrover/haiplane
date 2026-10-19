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


# ---- #1862: хаб сам просит прогон на новой голове ветки задачи в работе ----
#
# До сдачи у облачного исполнителя нет gh, и CI на его коммит никто не
# запускает. Проход поллера ci_head_run видит голову ветки задачи в running и
# один раз на (задача, sha) просит прогон; итог прогона после разрешения
# доверия пишет в ленту. Тесты ходят в ПРОХОД ПОЛЛЕРА, а не в вспомогательную
# функцию: AC говорит про циклы поллера.

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402

from hub import config  # noqa: E402
from tests.ci_trust_support import (  # noqa: E402
    SAT_CHECKS,
    SAT_CONTRACT,
    SAT_PATH,
    attach_fake_github,
)

HEAD_A = "a1" * 20
HEAD_B = "b2" * 20
HEAD_BRANCH = "task-1862/cloud-branch"
ASKED = "запрос CI принят для ветки"


class _CloudGit(NoopGitOps):
    """Git, в котором головы веток и прогоны задаёт тест, а запрос прогона пишется."""

    def __init__(self) -> None:
        super().__init__()
        self.heads: dict[str, str] = {}
        self.heads_down = False
        self.head_reads = 0
        self.requests: list[tuple[str, str | None]] = []
        self.outcomes: list[object] = []
        self.github = attach_fake_github(self)
        every_run = self.github.ci_runs_for_sha

        async def only_the_asked_workflow(
            head_sha,
            repo=None,
            gh_repo=None,
            forge="",
            workflow_path=".github/workflows/ci.yml",
        ):
            runs = await every_run(head_sha, repo, gh_repo, forge, workflow_path)
            if runs is None:
                return None
            return [r for r in runs if r["path"] == workflow_path]

        self.ci_runs_for_sha = only_the_asked_workflow  # type: ignore[method-assign]

    async def remote_heads(self, branches, repo=None):
        self.head_reads += 1
        if self.heads_down:
            return None
        return {b: self.heads[b] for b in branches if b in self.heads}

    async def request_ci_run(
        self, branch, repo=None, gh_repo=None, forge="", workflow_path=None
    ):
        self.requests.append((branch, workflow_path))
        step = (
            self.outcomes.pop(0)
            if self.outcomes
            else CIRunRequestResult(
                CIRunRequestOutcome.requested,
                "workflow_dispatched",
                details=workflow_path,
            )
        )
        if isinstance(step, BaseException):
            raise step
        return step  # type: ignore[return-value]


@pytest.fixture
def cloud(monkeypatch) -> _CloudGit:
    monkeypatch.setattr(config, "CI_HEAD_RUN_GRACE_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 3600, raising=False)
    monkeypatch.setattr(config, "CI_HEAD_RUN_TASKS_PER_CYCLE", 50, raising=False)
    git = _CloudGit()
    git.heads[HEAD_BRANCH] = HEAD_A
    plugins.git_ops = git
    return git


async def _cloud_task(
    db: aiosqlite.Connection,
    *,
    mode: str | None = "require",
    forge: str = "github",
    status: str = "running",
    slug: str = "cloud",
    branch: str = HEAD_BRANCH,
) -> int:
    policy: dict = {"ci_contract": SAT_CONTRACT}
    if mode is not None:
        policy["ci_before_submit"] = mode
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(
        db, pid, gate_policy=json.dumps(policy), repo="own/rep", forge=forge
    )
    task_id = await repo.create_task(
        db,
        title="Cloud task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, project_id=pid, branch=branch)
    await db.commit()
    return task_id


async def _cycle(conn: aiosqlite.Connection) -> None:
    """Один проход поллера ci_head_run на данном соединении."""
    from hub.poller import SWEEPS

    sweep = next((s for s in SWEEPS if s.name == "ci_head_run"), None)
    assert sweep is not None, "в конвейере поллера нет прохода ci_head_run"
    await sweep.run(conn)


def _restart() -> None:
    """Процесс хаба перезапущен: всё, что держалось в памяти, забыто."""
    if importlib.util.find_spec("hub.services.ci_head_run") is not None:
        from hub.services import ci_head_run

        ci_head_run.reset_state()


async def _notes(db: aiosqlite.Connection, task_id: int, needle: str) -> list[str]:
    return [
        u["content"]
        for u in (dict(r) for r in await repo.get_task_updates(db, task_id))
        if needle in (u.get("content") or "")
    ]


async def test_running_task_new_head_gets_one_ci_run(
    db: aiosqlite.Connection, db_dsn: str, cloud: _CloudGit
):
    """AC-1: ровно один запрос на (задача, sha) при гонке, рестарте и возврате A→B→A."""
    task_id = await _cloud_task(db)
    other = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await other.execute("PRAGMA busy_timeout = 5000")
        other.row_factory = aiosqlite.Row
        async with asyncio.timeout(30):
            await asyncio.gather(_cycle(db), _cycle(other))
        assert cloud.requests == [(HEAD_BRANCH, SAT_PATH)], (
            "два цикла на двух соединениях заказали один прогон на один коммит; "
            "запрос идёт workflow'ом из ci_contract проекта"
        )
        first = await _notes(db, task_id, ASKED)
        assert (
            len(first) == 1 and HEAD_A[:12] in first[0] and HEAD_BRANCH in first[0]
        ), "в ленте одна запись о запросе: ветка и наблюдаемый sha"

        _restart()
        await _cycle(db)
        assert len(cloud.requests) == 1, "рестарт не стирает журнал запросов"

        cloud.heads[HEAD_BRANCH] = HEAD_B
        await _cycle(db)
        assert [r[0] for r in cloud.requests] == [HEAD_BRANCH] * 2, (
            "новая голова — новый запрос"
        )
        cloud.heads[HEAD_BRANCH] = HEAD_A
        await _cycle(db)
        await _cycle(other)
        assert len(cloud.requests) == 2, "возврат к A третьего запроса не рождает"
        notes = await _notes(db, task_id, ASKED)
        assert len(notes) == 2 and HEAD_B[:12] in notes[1]
    finally:
        await other.close()


async def test_running_task_no_duplicate_or_foreign_runs(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-2: когда запроса быть не должно — его нет; когда должен — он есть."""
    # 1. прогон нужного workflow уже есть (запустил исполнитель)
    task_id = await _cloud_task(db, slug="has-run")
    cloud.github.attempt(task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS))
    await _cycle(db)
    assert cloud.requests == [], "прогон есть — просить нечего"
    assert (await repo.get_task(db, task_id))["ci_run_requested_sha"] in ("", None), (
        "попытка #1197 после сдачи не израсходована"
    )

    # 2. есть только чужой workflow — запрос нужен
    foreign = await _cloud_task(db, slug="foreign", branch="task-1/foreign")
    cloud.heads["task-1/foreign"] = HEAD_B
    cloud.github.attempt(foreign, HEAD_B, path=".github/workflows/release.yml")
    await _cycle(db)
    assert cloud.requests == [("task-1/foreign", SAT_PATH)], (
        "чужой workflow — не прогон тестов проекта"
    )

    # 2b. grace не истёк: голову увидели, исполнитель мог запустить CI сам
    cloud.requests.clear()
    monkeypatch.setattr(config, "CI_HEAD_RUN_GRACE_SECONDS", 3600)
    cloud.heads["task-1b/waits"] = "9a" * 20
    await _cloud_task(db, slug="waits", branch="task-1b/waits")
    await _cycle(db)
    assert f"runs:{'9a' * 20}" not in cloud.github.calls, (
        "в окне grace хаб не ходит к форжу за прогонами"
    )
    assert [r for r in cloud.requests if r[0] == "task-1b/waits"] == [], (
        "окно grace не истекло - запроса нет"
    )
    monkeypatch.setattr(config, "CI_HEAD_RUN_GRACE_SECONDS", 0)
    await _cycle(db)
    assert [r[0] for r in cloud.requests if r[0] == "task-1b/waits"] == [
        "task-1b/waits"
    ], "окно истекло - запрос ушёл"

    # 3. не выходит за рамки: grace, режим off и без ключа, GitVerse, статус
    cloud.requests.clear()
    cloud.heads["task-2/grace"] = "c3" * 20
    await _cloud_task(db, slug="grace", branch="task-2/grace")
    cases = [
        ("off", dict(mode="off")),
        ("nokey", dict(mode=None)),
        ("gitverse", dict(forge="gitverse")),
        ("review", dict(status="review")),
    ]
    for slug, extra in cases:
        branch = f"task-3/{slug}"
        cloud.heads[branch] = "d4" * 20
        await _cloud_task(db, slug=slug, branch=branch, **extra)
    monkeypatch.setattr(config, "CI_HEAD_RUN_GRACE_SECONDS", 3600)
    await _cycle(db)
    monkeypatch.setattr(config, "CI_HEAD_RUN_GRACE_SECONDS", 0)
    await _cycle(db)
    asked = {r[0] for r in cloud.requests}
    assert asked == {"task-2/grace"}, (
        f"запросили {sorted(asked)}: только grace-задача, истёкший срок, "
        "остальные случаи молчат (off, нет ключа, GitVerse, не running)"
    )
    rows = await db.execute(
        "SELECT count(*) FROM ci_head_runs WHERE branch LIKE 'task-3/%'"
    )
    assert (await rows.fetchone())[0] == 0, "вне области — без записей журнала"

    # 4. неоднозначный исход: резерв держится, не больше двух запросов
    cloud.requests.clear()
    cloud.outcomes = [
        CIRunRequestResult(CIRunRequestOutcome.unavailable, "dispatch_call_failed"),
        TimeoutError("после отправки"),
    ]
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 3600)
    amb = await _cloud_task(db, slug="amb", branch="task-4/amb")
    cloud.heads["task-4/amb"] = "e5" * 20
    for _ in range(3):
        await _cycle(db)
    assert [r for r in cloud.requests if r[0] == "task-4/amb"] == [
        ("task-4/amb", SAT_PATH)
    ], "срок повтора не истёк: резерв держится"
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    for _ in range(4):
        await _cycle(db)
    assert len([r for r in cloud.requests if r[0] == "task-4/amb"]) == 2, (
        "всего не больше двух запросов на (задача, sha)"
    )
    assert (await repo.get_task(db, amb))["ci_run_requested_sha"] in ("", None)

    # 5. неоднозначный исход, а прогон потом появился — повтора нет
    cloud.requests.clear()
    cloud.outcomes = [TimeoutError("после отправки")]
    late = await _cloud_task(db, slug="late", branch="task-5/late")
    cloud.heads["task-5/late"] = "f6" * 20
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    await _cycle(db)
    cloud.github.attempt(late, "f6" * 20, path=SAT_PATH, checks=dict(SAT_CHECKS))
    for _ in range(3):
        await _cycle(db)
    assert len([r for r in cloud.requests if r[0] == "task-5/late"]) == 1, (
        "прогон появился — повторять запрос нечего"
    )


async def _announced(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return await _notes(db, task_id, "CI на sha")


async def test_ci_report_for_running_head_is_announced_once(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """AC-3: итог объявляется один раз на (задача, sha, попытка) после доверия."""
    from hub.services import ci_trust
    from hub.services.ci_report import accept_ci_run_report

    task_id = await _cloud_task(db)
    await _cycle(db)

    def report(**kw):
        return cloud.github.attempt(
            task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), **kw
        )

    # отчёт пришёл, а GitHub недоступен: доверия нет — молчим
    first = report(run_id="7001")
    cloud.github.down = True
    await accept_ci_run_report(db, task_id, **first)
    await _cycle(db)
    assert await _announced(db, task_id) == [], "непроверенный отчёт не объявляется"

    # доверие разрешилось позже (проход ci_trust): объявление ровно одно
    cloud.github.down = False
    standing = await ci_trust.refresh(db, task_id, HEAD_A)
    assert standing is not None and standing.verified
    await _cycle(db)
    await _cycle(db)
    notes = await _announced(db, task_id)
    assert len(notes) == 1, "повторные циклы и повторный отчёт не множат запись"
    assert HEAD_A[:12] in notes[0] and "success" in notes[0] and "7001" in notes[0]
    assert "голова уже другая" not in notes[0]

    await accept_ci_run_report(db, task_id, **first)
    await _cycle(db)
    assert len(await _announced(db, task_id)) == 1, "тот же attempt — та же запись"

    # новая попытка того же прогона — отдельная запись
    second = report(run_id="7001", attempt="2", conclusion="failure")
    await accept_ci_run_report(db, task_id, **second)
    await _cycle(db)
    notes = await _announced(db, task_id)
    assert len(notes) == 2 and "failure" in notes[1] and "попытка 2" in notes[1]

    # отчёт для устаревшей головы: голова ушла, запись помечена
    cloud.heads[HEAD_BRANCH] = HEAD_B
    await _cycle(db)
    old = report(run_id="7002")
    await accept_ci_run_report(db, task_id, **old)
    await _cycle(db)
    notes = await _announced(db, task_id)
    assert len(notes) == 3 and "голова уже другая" in notes[2]


async def test_the_dispatch_goes_to_the_workflow_named_by_the_contract(monkeypatch):
    """#1862: workflow из ci_contract - единственный, что дёргается; нет такого - отказ."""
    calls: list[tuple[str, ...]] = []

    async def fake_run(*cmd, **kw):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "dispatches" in joined:
            return 0, "", ""
        return (
            0,
            '{"workflows": ['
            '{"id": 1, "state": "active", "path": ".github/workflows/ci.yml"},'
            '{"id": 2, "state": "active", "path": ".github/workflows/tests.yml"}]}',
            "",
        )

    monkeypatch.setattr("hub.integrations.proc.run", fake_run)
    forge = GitHubForge()

    got = await forge.request_ci_run(
        "task-1/x", gh_repo="own/rep", workflow_path=".github/workflows/tests.yml"
    )
    assert got.outcome is CIRunRequestOutcome.requested
    assert got.details == ".github/workflows/tests.yml"
    assert any("workflows/2/dispatches" in " ".join(c) for c in calls), (
        "контракт назвал tests.yml - запрос идёт ему, а не предпочтению хаба"
    )
    assert not any("workflows/1/dispatches" in " ".join(c) for c in calls)

    calls.clear()
    missing = await forge.request_ci_run(
        "task-1/x", gh_repo="own/rep", workflow_path=".github/workflows/absent.yml"
    )
    assert missing.outcome is CIRunRequestOutcome.declined
    assert missing.reason == "no_known_ci_workflow"
    assert not any("dispatches" in " ".join(c) for c in calls)


async def test_remote_heads_reads_every_branch_in_one_call(monkeypatch):
    """#1862: одно чтение на репозиторий; «не ответил» (None) не равно «веток нет» ({})."""
    from hub.integrations import git_ops as git_ops_mod

    seen: list[tuple[str, ...]] = []
    reply: dict[str, object] = {"rc": 0, "out": ""}

    async def fake_git(*args, **kw):
        seen.append(args)
        return reply["rc"], reply["out"], ""

    monkeypatch.setattr(git_ops_mod, "_git", fake_git)
    ops = git_ops_mod.GitOpsIntegration()

    reply["out"] = f"{HEAD_A}\trefs/heads/task-1/a\n{HEAD_B}\trefs/heads/task-2/b\n"
    got = await ops.remote_heads(["task-1/a", "task-2/b", "task-3/gone"], repo="/r")
    assert got == {"task-1/a": HEAD_A, "task-2/b": HEAD_B}
    assert len(seen) == 1, "три ветки - один ls-remote"
    assert all(
        f"refs/heads/{b}" in seen[0] for b in ("task-1/a", "task-2/b", "task-3/gone")
    ), "в одном вызове названы все ветки"

    reply["out"] = ""
    assert await ops.remote_heads(["task-3/gone"], repo="/r") == {}
    reply["rc"] = 128
    assert await ops.remote_heads(["task-3/gone"], repo="/r") is None
    assert await ops.remote_heads([], repo="/r") == {}


async def test_the_real_git_ops_hands_the_workflow_to_the_forge(monkeypatch):
    """#1862: GitOpsIntegration.request_ci_run не теряет workflow_path по дороге к форжу."""
    from hub.integrations import git_ops as git_ops_mod

    seen: dict[str, object] = {}

    class _Forge:
        async def request_ci_run(self, branch, *, repo=None, gh_repo=None, **kw):
            seen.update(kw)
            return CIRunRequestResult(CIRunRequestOutcome.requested, "ok")

    ops = git_ops_mod.GitOpsIntegration()
    monkeypatch.setattr(ops, "_forge_for", lambda forge="": _Forge())
    await ops.request_ci_run(
        "task-1/x", gh_repo="own/rep", workflow_path=".github/workflows/tests.yml"
    )
    assert seen == {"workflow_path": ".github/workflows/tests.yml"}


async def test_a_failed_head_read_backs_off_instead_of_asking_every_cycle(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1862: git не ответил - отступ, а не повтор вопроса на каждом цикле (образец ci_trust)."""
    await _cloud_task(db)
    cloud.heads_down = True
    await _cycle(db)
    await _cycle(db)
    assert cloud.head_reads == 1, "после отказа отступаем, а не спрашиваем снова"
    assert cloud.requests == [], "голов не видно - вслепую не просим"


async def test_a_verdict_under_a_changed_contract_is_not_announced(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1862: подтверждение выдано под контрактом, которого уже нет, - не объявляем."""
    from hub.services.ci_report import accept_ci_run_report
    from tests.ci_trust_support import SAT_CONTRACT_C, set_contract

    task_id = await _cloud_task(db)
    await _cycle(db)
    sent = cloud.github.attempt(
        task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="8001"
    )
    await accept_ci_run_report(db, task_id, **sent)
    project = await repo.resolve_project_for_task(db, task_id)
    await set_contract(db, project["id"], SAT_CONTRACT_C)
    await db.commit()
    await _cycle(db)
    assert await _announced(db, task_id) == []


async def test_a_cycle_serves_a_bounded_number_of_tasks_in_turn(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862: не больше N задач за цикл, круговая очередь, одно чтение голов на репозиторий."""
    monkeypatch.setattr(config, "CI_HEAD_RUN_TASKS_PER_CYCLE", 2)
    branches = [f"task-7/b{i}" for i in range(5)]
    for i, branch in enumerate(branches):
        cloud.heads[branch] = f"{i:02d}" * 20
        await _cloud_task(db, slug=f"turn{i}", branch=branch)
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == branches[:2]
    # одно чтение голов на репозиторий + по одному контрольному после каждого
    # принятого запроса (голова могла сдвинуться, пока шёл запрос)
    assert cloud.head_reads == 1 + 2
    await _cycle(db)
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == branches, (
        "по кругу: хвост очереди не голодает, и никто не получает запрос дважды"
    )


async def test_two_announcers_with_the_same_stale_list_write_one_entry(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862: дедуп держит журнал, а не выборка: устаревший список не плодит записи."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    task_id = await _cloud_task(db)
    await _cycle(db)
    sent = cloud.github.attempt(
        task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="9001"
    )
    await accept_ci_run_report(db, task_id, **sent)
    stale_list = await journal.unannounced_verified(db)
    assert len(stale_list) == 1
    monkeypatch.setattr(
        journal, "unannounced_verified", AsyncMock(return_value=stale_list)
    )
    await ci_head_run.announce_results(db)
    await ci_head_run.announce_results(db)
    assert len(await _announced(db, task_id)) == 1
