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

import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import brand
from hub.db import write_transaction
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
        self.broken_clones: set[str] = set()
        self.raising_clones: dict[str, BaseException] = {}
        self.head_delay = 0.0
        self.request_delay = 0.0
        self.open_prs: dict[str, int] = {}
        self.pr_errors: set[str] = set()
        self.on_heads = None  # type: ignore[var-annotated]
        self.probes = 0
        self.on_probe = None  # type: ignore[var-annotated]
        self.on_request = None  # type: ignore[var-annotated]
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
            self.probes += 1
            if self.on_probe is not None:
                await self.on_probe()
            runs = await every_run(head_sha, repo, gh_repo, forge, workflow_path)
            if runs is None:
                return None
            return [r for r in runs if r["path"] == workflow_path]

        self.ci_runs_for_sha = only_the_asked_workflow  # type: ignore[method-assign]

    async def remote_heads(self, branches, repo=None):
        self.head_reads += 1
        if self.on_heads is not None:
            await self.on_heads()
        if self.head_delay:
            await asyncio.sleep(self.head_delay)
        if repo in self.raising_clones:
            raise self.raising_clones[repo]
        if self.heads_down or repo in self.broken_clones:
            return None
        return {b: self.heads[b] for b in branches if b in self.heads}

    async def open_pr_between(self, base, head, repo=None, gh_repo=None, forge=""):
        if head in self.pr_errors:
            return (None, f"список PR {head} → {base} не прочитан")
        return (self.open_prs.get(head), "")

    async def request_ci_run(
        self,
        branch,
        repo=None,
        gh_repo=None,
        forge="",
        workflow_path=None,
        on_send=None,
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
        # Подмена - это "POST": отправка начинается сразу, если сценарий не
        # говорит, что форж отказал ДО неё (список workflow не прочитан и т.п.).
        before_post = (
            isinstance(step, CIRunRequestResult)
            and step.outcome
            in (CIRunRequestOutcome.unavailable, CIRunRequestOutcome.declined)
            and step.reason
            not in ("dispatch_call_failed", "workflow_dispatch_not_declared_on_ref")
        )
        if on_send is not None and not before_post:
            on_send()
        if self.request_delay:
            await asyncio.sleep(self.request_delay)
        if self.on_request is not None:
            await self.on_request()
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
    gh_repo: str = "own/rep",
    workspace: str = "",
    contract: bool = True,
) -> int:
    policy: dict = {"ci_contract": SAT_CONTRACT} if contract else {}
    if mode is not None:
        policy["ci_before_submit"] = mode
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(
        db,
        pid,
        gate_policy=json.dumps(policy),
        repo=gh_repo,
        forge=forge,
        workspace_path=workspace,
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
        ("nocontract", dict(contract=False)),
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
        _restart()  # отступ после неоднозначного исхода истёк
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
    _restart()
    await _cycle(db)
    cloud.github.attempt(late, "f6" * 20, path=SAT_PATH, checks=dict(SAT_CHECKS))
    _restart()
    for _ in range(3):
        await _cycle(db)
    assert len([r for r in cloud.requests if r[0] == "task-5/late"]) == 1, (
        "прогон появился — повторять запрос нечего"
    )


async def _announced(db: aiosqlite.Connection, task_id: int) -> list[str]:
    return await _notes(db, task_id, "CI на sha")


async def test_ci_report_for_running_head_is_announced_once(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
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

    # подтверждение под прежним контрактом не объявляется; устаревший список
    # выборки не плодит запись (дедуп держит журнал, а не выборка)
    await _contract_change_blocks_announcement(db, cloud)
    await _snapshot_gone_stale_blocks_announcement(db, cloud, monkeypatch)
    await _stale_list_writes_one_entry(db, cloud, monkeypatch)


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
    assert seen == {"workflow_path": ".github/workflows/tests.yml", "on_send": None}


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


async def _contract_change_blocks_announcement(
    db: aiosqlite.Connection, cloud: _CloudGit
) -> None:
    """Подтверждение выдано под контрактом, которого уже нет, - не объявляем."""
    _restart()  # отступ предыдущего шага (форж «лежал») не относится к этому
    from hub.services.ci_report import accept_ci_run_report
    from tests.ci_trust_support import SAT_CONTRACT_C, set_contract

    branch = "task-8/contract"
    cloud.heads[branch] = HEAD_A
    task_id = await _cloud_task(db, slug="contract", branch=branch)
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


async def _snapshot_gone_stale_blocks_announcement(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
) -> None:
    """(Codex) Отчёт потерял подтверждение между выборкой и записью - не объявляем."""
    _restart()  # отступ предыдущего шага (форж «лежал») не относится к этому
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    branch = "task-10/snapshot"
    cloud.heads[branch] = HEAD_A
    task_id = await _cloud_task(db, slug="snapshot", branch=branch)
    await _cycle(db)
    sent = cloud.github.attempt(
        task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="9101"
    )
    await accept_ci_run_report(db, task_id, **sent)
    snapshot = [
        r for r in await journal.unannounced_verified(db) if r["task_id"] == task_id
    ]
    assert len(snapshot) == 1
    await db.execute(
        "UPDATE ci_report_attempts SET trust='unverified', trust_reason='replaced' "
        "WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    real = journal.unannounced_verified
    monkeypatch.setattr(
        journal, "unannounced_verified", AsyncMock(return_value=snapshot)
    )
    await ci_head_run.announce_results(db)
    monkeypatch.setattr(journal, "unannounced_verified", real)
    assert await _announced(db, task_id) == []


async def _stale_list_writes_one_entry(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
) -> None:
    """Дедуп держит журнал, а не выборка: устаревший список не плодит записи."""
    _restart()  # отступ предыдущего шага (форж «лежал») не относится к этому
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    branch = "task-9/stale-list"
    cloud.heads[branch] = HEAD_A
    task_id = await _cloud_task(db, slug="stale-list", branch=branch)
    await _cycle(db)
    sent = cloud.github.attempt(
        task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="9001"
    )
    await accept_ci_run_report(db, task_id, **sent)
    stale_list = [
        r for r in await journal.unannounced_verified(db) if r["task_id"] == task_id
    ]
    assert len(stale_list) == 1
    monkeypatch.setattr(
        journal, "unannounced_verified", AsyncMock(return_value=stale_list)
    )
    await ci_head_run.announce_results(db)
    await ci_head_run.announce_results(db)
    assert len(await _announced(db, task_id)) == 1


async def test_a_head_that_moves_during_the_request_is_not_requested_twice(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862 (Codex): запрос идёт по ref; сдвинутая голова B не получает второй запрос."""

    async def branch_moves():
        cloud.heads[HEAD_BRANCH] = HEAD_B

    cloud.on_request = branch_moves
    task_id = await _cloud_task(db)
    await _cycle(db)
    assert len(cloud.requests) == 1
    moved = await _notes(db, task_id, "сдвинулась")
    assert len(moved) == 1 and HEAD_A[:12] in moved[0] and HEAD_B[:12] in moved[0]
    cloud.on_request = None
    await _cycle(db)
    await _cycle(db)
    assert len(cloud.requests) == 1, (
        "B уже записана как «запрошена»: её прогон ищут, второй не заказывают"
    )
    # голова вернулась на A, а прогона по ней нет (CI шёл по B): A не потеряна
    cloud.heads[HEAD_BRANCH] = HEAD_A
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    await _cycle(db)
    assert len(cloud.requests) == 2, (
        "A вернулась в «исход неизвестен» и получила повтор"
    )


async def test_the_reservation_rechecks_that_the_task_is_still_in_scope(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1862 (Codex): пока шла проба, задача ушла из running - запрос не уходит."""
    task_id = await _cloud_task(db)

    async def task_moves_on():
        await db.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        await db.commit()

    cloud.on_probe = task_moves_on
    await _cycle(db)
    assert cloud.requests == [], "статус сменился между выборкой и резервом"


async def test_the_journal_state_and_the_feed_entry_stand_or_fall_together(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862 (Codex): сбой записи в ленту не оставляет «requested» без записи."""
    task_id = await _cloud_task(db)
    real = repo.add_task_update

    async def broken(*args, **kwargs):
        raise RuntimeError("лента недоступна")

    monkeypatch.setattr(repo, "add_task_update", broken)
    await _cycle(db)
    monkeypatch.setattr(repo, "add_task_update", real)
    cur = await db.execute("SELECT state FROM ci_head_runs WHERE task_id=?", (task_id,))
    assert (await cur.fetchone())[0] == "reserved", (
        "состояние откатилось вместе с записью: исход остаётся неоднозначным"
    )


async def test_a_foreign_repository_without_a_clone_is_out_of_scope(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1862 (Codex): без клона головы читались бы из клона хаба, а запрос ушёл бы в чужой репо."""
    await _cloud_task(db, gh_repo="another/project", workspace="")
    await _cycle(db)
    assert cloud.head_reads == 0 and cloud.requests == []
    await _cloud_task(db, slug="cloned", gh_repo="another/project", workspace="/srv/x")
    await _cycle(db)
    assert len(cloud.requests) == 1, "с клоном проекта читаем оттуда"


async def test_the_announcement_queue_is_read_page_by_page(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862 (Codex): пачка необъявляемых строк не вытесняет объявляемую за границу страницы."""
    from hub import repository_ci_head as journal
    from hub.services.ci_report import accept_ci_run_report
    from tests.ci_trust_support import SAT_CONTRACT_C, set_contract

    cloud.heads["task-6/old"] = HEAD_A
    cloud.heads["task-6/newer"] = HEAD_B
    good = await _cloud_task(db, slug="good", branch="task-6/old")
    bad = await _cloud_task(db, slug="bad", branch="task-6/newer")
    await _cycle(db)
    for task_id, sha, run in ((good, HEAD_A, "6001"), (bad, HEAD_B, "6002")):
        sent = cloud.github.attempt(
            task_id, sha, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id=run
        )
        await accept_ci_run_report(db, task_id, **sent)
    project = await repo.resolve_project_for_task(db, bad)
    await set_contract(db, project["id"], SAT_CONTRACT_C)
    await db.commit()
    monkeypatch.setattr(journal, "ANNOUNCE_PAGE", 1)
    await _cycle(db)
    assert len(await _announced(db, good)) == 1, "второй странице тоже доходит очередь"
    assert await _announced(db, bad) == []


async def test_a_slow_cycle_stops_starting_tasks_and_a_failed_send_backs_off(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """#1862 (Codex): бюджет прохода; после неоднозначного исхода форж не трогаем."""
    from hub.services import ci_head_run

    await _cloud_task(db)
    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", -1.0)
    await _cycle(db)
    assert cloud.requests == [], "бюджет прохода исчерпан - новых задач не начинаем"
    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", 45.0)
    cloud.outcomes = [TimeoutError("после отправки")]
    await _cycle(db)
    assert len(cloud.requests) == 1
    reads = cloud.head_reads
    await _cycle(db)
    assert cloud.head_reads == reads, "после неоднозначного исхода - отступ"


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
    # одно чтение голов на репозиторий + одно контрольное на ВСЕ принятые запросы
    # прохода (голова могла сдвинуться, пока шёл запрос)
    assert cloud.head_reads == 1 + 1
    await _cycle(db)
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == branches, (
        "по кругу: хвост очереди не голодает, и никто не получает запрос дважды"
    )


async def test_an_unreachable_clone_does_not_hold_the_queue_of_the_others(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1862 (Codex): клон без ответа выпадает из очереди, здоровые проекты идут."""
    cloud.broken_clones.add("/broken")
    cloud.heads["task-11/broken"] = HEAD_A
    cloud.heads["task-11/healthy"] = HEAD_B
    await _cloud_task(db, slug="broken", branch="task-11/broken", workspace="/broken")
    await _cloud_task(
        db, slug="healthy", branch="task-11/healthy", workspace="/healthy"
    )
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == ["task-11/healthy"]
    reads = cloud.head_reads
    await _cycle(db)
    assert cloud.head_reads == reads + 1, "сломанный клон не спрашивают, здоровый - да"


# ---- #1862, вердикт 1: общий дедлайн, изоляция, PR, сверка прогона, политика, курсор ----


async def test_slow_clones_stay_inside_the_budget_of_the_whole_pass(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """P1: дедлайн на весь проход с самого начала; сеть получает оставшееся время."""
    from hub.services import ci_head_run

    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", 0.6)
    cloud.head_delay = 5.0
    for i in range(4):
        branch = f"task-12/slow{i}"
        cloud.heads[branch] = f"{i:02d}" * 20
        await _cloud_task(db, slug=f"slow{i}", branch=branch, workspace=f"/clone{i}")
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(6):
        await _cycle(db)
    assert asyncio.get_running_loop().time() - started < 3.0, (
        "четыре медленных клона не выходят за бюджет прохода"
    )

    # то же для платных вызовов: dispatch не длиннее остатка бюджета
    cloud.head_delay = 0.0
    cloud.request_delay = 5.0
    ci_head_run.reset_state()
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(6):
        await _cycle(db)
    assert asyncio.get_running_loop().time() - started < 3.0


async def test_a_broken_clone_does_not_stop_the_pass_for_the_healthy_one(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """Исключение клона (FileNotFoundError и любое) - отказ ЭТОГО клона."""
    cloud.raising_clones["/gone"] = FileNotFoundError("/gone")
    cloud.heads["task-13/gone"] = HEAD_A
    cloud.heads["task-13/ok"] = HEAD_B
    await _cloud_task(db, slug="gone", branch="task-13/gone", workspace="/gone")
    await _cloud_task(db, slug="okay", branch="task-13/ok", workspace="/ok")
    await _cycle(db)
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == ["task-13/ok"]
    cloud.raising_clones["/gone"] = RuntimeError("любое другое")
    cloud.heads["task-13/ok2"] = HEAD_A
    await _cloud_task(db, slug="okay2", branch="task-13/ok2", workspace="/ok")
    from hub.services import ci_head_run

    ci_head_run.reset_state()
    await _cycle(db)
    assert "task-13/ok2" in [r[0] for r in cloud.requests]


async def test_no_dispatch_for_an_open_pr_or_a_run_that_appeared_meanwhile(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """Открытый PR сам запускает CI; перед dispatch прогон ищется ещё раз."""
    cloud.heads["task-14/pr"] = HEAD_A
    cloud.open_prs["task-14/pr"] = 7
    await _cloud_task(db, slug="haspr", branch="task-14/pr")
    await _cycle(db)
    assert cloud.requests == [], "у ветки открытый PR"

    cloud.heads["task-14/manual"] = HEAD_B
    manual = await _cloud_task(db, slug="manual", branch="task-14/manual")

    before = cloud.probes

    async def executor_runs_it_by_hand():
        if cloud.probes >= before + 2 and not cloud.github.runs:
            cloud.github.attempt(manual, HEAD_B, path=SAT_PATH, checks=dict(SAT_CHECKS))

    cloud.on_probe = executor_runs_it_by_hand
    await _cycle(db)
    assert cloud.requests == [], "ручной прогон появился между пробой и dispatch"


async def test_a_run_that_landed_on_another_sha_hands_the_request_back(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """A->B->A: dispatch по ref попал на B; A без прогона заказывается снова, но не больше двух."""
    task_id = await _cloud_task(db)

    async def lands_on_b():
        cloud.heads[HEAD_BRANCH] = HEAD_B
        cloud.github.attempt(task_id, HEAD_B, path=SAT_PATH, checks=dict(SAT_CHECKS))

    cloud.on_request = lands_on_b
    await _cycle(db)
    assert len(cloud.requests) == 1
    cloud.on_request = None
    cloud.heads[HEAD_BRANCH] = HEAD_A
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    from hub.services import ci_head_run

    for _ in range(4):
        ci_head_run.reset_state()
        await _cycle(db)
    asked = [r for r in cloud.requests]
    assert len(asked) == 2, "прогона на A нет: заявка возвращена, но лимит два"


async def test_the_policy_is_reread_inside_the_reservation(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """ci_before_submit переключили в off во время прохода - dispatch не уходит."""
    task_id = await _cloud_task(db)

    async def switched_off():
        project = await repo.resolve_project_for_task(db, task_id)
        policy = {"ci_contract": SAT_CONTRACT, "ci_before_submit": "off"}
        await repo.update_project(db, project["id"], gate_policy=json.dumps(policy))
        await db.commit()

    cloud.on_probe = switched_off
    await _cycle(db)
    assert cloud.requests == []


async def test_old_good_report_is_announced_behind_a_pile_of_unfit_rows(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """1 подходящий старый отчёт и 200 новых со старым контрактом - объявление появляется."""
    from hub import repository_ci_head as journal
    from hub.services.ci_report import accept_ci_run_report
    from tests.ci_trust_support import SAT_CONTRACT_C, set_contract

    monkeypatch.setattr(journal, "ANNOUNCE_PAGE", 20)
    monkeypatch.setattr(journal, "ANNOUNCE_PAGES", 2)
    cloud.heads["task-15/old"] = HEAD_A
    good = await _cloud_task(db, slug="oldgood", branch="task-15/old")
    await _cycle(db)
    await accept_ci_run_report(
        db,
        good,
        **cloud.github.attempt(
            good, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="1501"
        ),
    )
    bad = await _cloud_task(db, slug="pile", branch="task-15/pile")
    project = await repo.resolve_project_for_task(db, bad)
    await set_contract(db, project["id"], SAT_CONTRACT_C)
    for i in range(200):
        sha = f"{i:040x}"
        await db.execute(
            "INSERT INTO ci_head_runs (task_id, sha, branch, state) "
            "VALUES (?, ?, 'task-15/pile', 'observed')",
            (bad, sha),
        )
        await db.execute(
            "INSERT INTO ci_report_attempts (task_id, head_sha, run_id, run_attempt, "
            "trust, trust_reason, trust_final, contract_fp) "
            "VALUES (?, ?, ?, '1', 'verified', '', 1, 'old-contract')",
            (bad, sha, str(9000 + i)),
        )
    await db.commit()
    for _ in range(12):
        await _cycle(db)
    assert len(await _announced(db, good)) == 1


async def test_a_failing_request_half_does_not_cancel_the_announcements(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """Половины прохода изолированы: сбой запросов не отменяет объявления."""
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    task_id = await _cloud_task(db)
    await _cycle(db)
    await accept_ci_run_report(
        db,
        task_id,
        **cloud.github.attempt(
            task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="1701"
        ),
    )

    async def broken(*args, **kwargs):
        raise RuntimeError("половина запросов упала")

    monkeypatch.setattr(ci_head_run, "request_for_new_heads", broken)
    await _cycle(db)
    assert len(await _announced(db, task_id)) == 1


async def _state_of(db: aiosqlite.Connection, task_id: int) -> str:
    cur = await db.execute("SELECT state FROM ci_head_runs WHERE task_id=?", (task_id,))
    return (await cur.fetchone())[0]


async def test_unknown_is_not_none_before_the_dispatch(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """P2 (Codex): не прочитанный список прогонов или PR - не «нет»: dispatch не делается."""
    from hub.services import ci_head_run

    # прогоны на повторной проверке не прочитались
    first = await _cloud_task(db, slug="unk-runs", branch="task-16/runs")
    cloud.heads["task-16/runs"] = HEAD_A
    before = cloud.probes

    async def forge_fails_on_second_probe():
        if cloud.probes >= before + 2:
            cloud.github.down = True

    cloud.on_probe = forge_fails_on_second_probe
    await _cycle(db)
    assert cloud.requests == []
    assert await _state_of(db, first) == "seen", "заявка не закрыта и не потрачена"
    # два «не знаю» подряд не исчерпывают попытки: dispatch не было ни разу
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    for _ in range(3):
        ci_head_run.reset_state()
        cloud.github.down = False
        cloud.on_probe = forge_fails_on_second_probe
        before = cloud.probes
        await _cycle(db)
    assert cloud.requests == []
    assert await _state_of(db, first) == "seen"
    cloud.on_probe = None
    cloud.github.down = False
    ci_head_run.reset_state()
    await _cycle(db)
    assert [r[0] for r in cloud.requests] == ["task-16/runs"]

    # список PR не прочитался
    cloud.requests.clear()
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 3600)
    second = await _cloud_task(db, slug="unk-pr", branch="task-16/pr")
    cloud.heads["task-16/pr"] = HEAD_B
    cloud.pr_errors.add("task-16/pr")
    ci_head_run.reset_state()
    await _cycle(db)
    assert cloud.requests == []
    assert await _state_of(db, second) == "seen"
    cloud.pr_errors.clear()
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    ci_head_run.reset_state()
    await _cycle(db)
    assert [r[0] for r in cloud.requests if r[0] == "task-16/pr"] == ["task-16/pr"]


async def test_the_task_is_rechecked_right_before_the_dispatch(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """P2 (Codex): после резерва задача ушла в review со сдачей - dispatch не уходит."""
    task_id = await _cloud_task(db)
    before = cloud.probes

    async def task_gets_submitted():
        if cloud.probes >= before + 2:
            await db.execute(
                "UPDATE tasks SET status='review', submission_generation=1 WHERE id=?",
                (task_id,),
            )
            await db.commit()

    cloud.on_probe = task_gets_submitted
    await _cycle(db)
    assert cloud.requests == []


async def test_a_ci_result_that_never_comes_is_said_aloud_once(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """Разбор 2б: за срок итога нет - запись, alert и уведомление, один раз на заявку."""
    from hub.services.ci_report import accept_ci_run_report

    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 60, raising=False)
    silent = await _cloud_task(db, slug="silent", branch="task-17/silent")
    cloud.heads["task-17/silent"] = HEAD_A
    answered = await _cloud_task(db, slug="answered", branch="task-17/answered")
    cloud.heads["task-17/answered"] = HEAD_B
    await _cycle(db)
    await accept_ci_run_report(
        db,
        answered,
        **cloud.github.attempt(
            answered, HEAD_B, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="1701"
        ),
    )
    await _cycle(db)
    assert await _notes(db, silent, "CI не пришёл") == [], "срок ещё не вышел"

    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 0)
    await _cycle(db)
    await _cycle(db)
    said = await _notes(db, silent, "CI не пришёл")
    assert len(said) == 1 and HEAD_A[:12] in said[0] and "run не найден" in said[0]
    kinds = [dict(u)["kind"] for u in await repo.get_task_updates(db, silent)]
    assert "alert" in kinds, "это alert, а не обычная строка ленты"
    cur = await db.execute(
        "SELECT count(*) FROM agent_messages WHERE related_task_id=?", (silent,)
    )
    assert (await cur.fetchone())[0] == 1, "уведомление ответственному - одно"
    assert await _notes(db, answered, "CI не пришёл") == [], "итог пришёл вовремя"
    assert len(await _announced(db, answered)) == 1


async def test_overdue_covers_exhausted_requests_and_the_current_head_only(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """P2 (Codex): «прогон так и не появился» тоже слышен; ждут ТЕКУЩУЮ голову (A->B->A)."""
    from hub.services import ci_head_run

    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 60, raising=False)
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    task_id = await _cloud_task(db)
    for sha in (HEAD_A, HEAD_B, HEAD_A):
        cloud.heads[HEAD_BRANCH] = sha
        ci_head_run.reset_state()
        await _cycle(db)
    for _ in range(4):  # заявки исчерпаны, прогона нет
        ci_head_run.reset_state()
        await _cycle(db)
    assert await _state_of_sha(db, task_id, HEAD_A) == "exhausted"
    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 0)
    await _cycle(db)
    said = await _notes(db, task_id, "CI не пришёл")
    assert len(said) == 1 and HEAD_A[:12] in said[0], "ждут текущую голову A, не B"


async def _state_of_sha(db: aiosqlite.Connection, task_id: int, sha: str) -> str:
    cur = await db.execute(
        "SELECT state FROM ci_head_runs WHERE task_id=? AND sha=?", (task_id, sha)
    )
    return (await cur.fetchone())[0]


async def test_a_slow_clone_cannot_starve_the_announcements(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """P2 (Codex): чтение голов ради пометки ограничено отдельно и объявление не ждёт."""
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    monkeypatch.setattr(ci_head_run, "LABEL_BUDGET_SECONDS", 0.2)
    cloud.heads["task-18/slow"] = HEAD_A
    task_id = await _cloud_task(
        db, slug="slowlabel", branch="task-18/slow", workspace="/slow"
    )
    await _cycle(db)
    await accept_ci_run_report(
        db,
        task_id,
        **cloud.github.attempt(
            task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="1801"
        ),
    )
    await db.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    await db.commit()
    cloud.head_delay = 5.0
    ci_head_run.reset_state()
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(8):
        await _cycle(db)
    assert asyncio.get_running_loop().time() - started < 3.0
    assert len(await _announced(db, task_id)) == 1


async def test_a_policy_switched_off_after_the_reservation_gives_the_attempt_back(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """Политику выключили между резервом и dispatch: запроса нет, попытка не потрачена."""
    task_id = await _cloud_task(db, slug="lateoff", branch="task-19/lateoff")
    cloud.heads["task-19/lateoff"] = HEAD_A
    before = cloud.probes

    async def switched_off_late():
        if cloud.probes >= before + 2:
            project = await repo.resolve_project_for_task(db, task_id)
            policy = {"ci_contract": SAT_CONTRACT, "ci_before_submit": "off"}
            await repo.update_project(db, project["id"], gate_policy=json.dumps(policy))
            await db.commit()

    cloud.on_probe = switched_off_late
    await _cycle(db)
    assert cloud.requests == []
    assert await _state_of(db, task_id) == "seen"
    cur = await db.execute(
        "SELECT attempts FROM ci_head_runs WHERE task_id=?", (task_id,)
    )
    assert (await cur.fetchone())[0] == 0


# ---- #1862, вердикт 2: состояние при прерывании, область alert, возврат попытки, курсор ----


async def test_a_pass_never_leaves_the_shared_connection_inside_a_transaction(
    db: aiosqlite.Connection, db_dsn: str, cloud: _CloudGit, monkeypatch
):
    """I1 (P1): БД не отменяется снаружи; после возврата соединение поллера чисто."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run

    await _cloud_task(db)
    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(ci_head_run, "HARD_CAP_EXTRA_SECONDS", 0.05, raising=False)
    other = await aiosqlite.connect(db_dsn, uri=True)
    try:
        await other.execute("PRAGMA busy_timeout = 5000")
        await other.execute("BEGIN IMMEDIATE")  # чужая запись держит замок

        async def release_later():
            await asyncio.sleep(0.4)
            await other.rollback()

        releaser = asyncio.create_task(release_later())
        async with asyncio.timeout(10):
            await _cycle(db)
        await releaser
    finally:
        await other.close()
    await asyncio.sleep(0.2)
    assert not db.in_transaction, "после прохода соединение поллера не в транзакции"
    await journal.set_state(db, "probe", 7)
    await db.rollback()  # конец тика поллера
    assert await journal.get_state(db, "probe") == 7, "следующий шаг коммитится"


async def _requested_overdue(db, cloud, monkeypatch, slug, branch):
    """Задача с принятым запросом, чьё ожидание уже просрочено (срок 0)."""
    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 60, raising=False)
    cloud.heads[branch] = HEAD_A
    task_id = await _cloud_task(db, slug=slug, branch=branch)
    await _cycle(db)
    assert len(cloud.requests) >= 1
    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 0)
    return task_id


async def test_the_overdue_alert_rechecks_the_same_scope_as_the_dispatch(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I2: пять сценариев ложного alert + контрольный, где он законен."""
    from hub.services import ci_head_run

    async def policy(task_id, **values):
        project = await repo.resolve_project_for_task(db, task_id)
        await repo.update_project(db, project["id"], gate_policy=json.dumps(values))
        await db.commit()

    cases = {
        "off": lambda t: policy(t, ci_contract=SAT_CONTRACT, ci_before_submit="off"),
        "nocontract": lambda t: policy(t, ci_before_submit="require"),
    }

    async def submitted(t):
        await db.execute(
            "UPDATE tasks SET submission_generation=1, submission_sha='x' WHERE id=?",
            (t,),
        )
        await db.commit()

    async def other_branch(t):
        await db.execute("UPDATE tasks SET branch='task-20/other' WHERE id=?", (t,))
        await db.commit()

    async def head_moved(t):
        cloud.heads[f"task-20/{t}-moved"] = HEAD_B
        ci_head_run._back_off()  # проход запросов в отступе, а голова ушла

    cases["submitted"] = submitted
    cases["branch"] = other_branch
    cases["moved"] = head_moved
    for slug, mutate in cases.items():
        branch = f"task-20/{slug}"
        task_id = await _requested_overdue(db, cloud, monkeypatch, slug, branch)
        if slug == "moved":
            cloud.heads[branch] = HEAD_B
            ci_head_run._back_off()
        else:
            await mutate(task_id)
        await _cycle(db)
        assert await _notes(db, task_id, "CI не пришёл") == [], (
            f"сценарий {slug}: область изменилась - alert был бы ложным"
        )
        ci_head_run.reset_state()
    ok = await _requested_overdue(db, cloud, monkeypatch, "legit", "task-20/legit")
    await _cycle(db)
    assert len(await _notes(db, ok, "CI не пришёл")) == 1


async def test_a_deadline_that_ends_before_the_forge_call_gives_the_attempt_back(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I3: «не отправлено» - не «неоднозначно»; попытка не расходуется."""
    from hub.services import ci_head_run

    # потолок вызова форжа - 0: дедлайн кончается РОВНО перед отправкой
    monkeypatch.setattr(ci_head_run, "DISPATCH_CEILING_SECONDS", 0.0)
    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    task_id = await _cloud_task(db)
    for _ in range(3):
        ci_head_run.reset_state()
        await _cycle(db)
    assert cloud.requests == []
    cur = await db.execute(
        "SELECT attempts, state FROM ci_head_runs WHERE task_id=?", (task_id,)
    )
    row = await cur.fetchone()
    assert row[0] == 0 and row[1] == "seen", (
        "три прохода без отправки не тратят попытки"
    )


async def test_the_announce_cursor_follows_the_last_processed_row_when_interrupted(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I4: сценарий Codex - 3 непригодных + 1 пригодный, страница 3, бюджет на 2 строки."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report
    from tests.ci_trust_support import SAT_CONTRACT_C, set_contract

    monkeypatch.setattr(journal, "ANNOUNCE_PAGE", 3)
    cloud.heads["task-21/good"] = HEAD_A
    good = await _cloud_task(db, slug="goodrow", branch="task-21/good")
    await _cycle(db)
    await accept_ci_run_report(
        db,
        good,
        **cloud.github.attempt(
            good, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="2101"
        ),
    )
    bad = await _cloud_task(db, slug="badrows", branch="task-21/bad")
    project = await repo.resolve_project_for_task(db, bad)
    await set_contract(db, project["id"], SAT_CONTRACT_C)
    for i in range(3):
        sha = f"{i + 1:040x}"
        await db.execute(
            "INSERT INTO ci_head_runs (task_id, sha, branch, state) "
            "VALUES (?, ?, 'task-21/bad', 'observed')",
            (bad, sha),
        )
        await db.execute(
            "INSERT INTO ci_report_attempts (task_id, head_sha, run_id, run_attempt, "
            "trust, trust_reason, trust_final, contract_fp) "
            "VALUES (?, ?, ?, '1', 'verified', '', 1, 'old-contract')",
            (bad, sha, str(2200 + i)),
        )
    await db.commit()
    known = {good: HEAD_A, bad: HEAD_A}
    # установившийся режим: всё, что есть, уже прошло через голову очереди
    await journal.set_state(db, journal.ANNOUNCE_TOP, await journal.top_attempt_id(db))
    calls = {"n": 0}
    real_left = ci_head_run._Deadline.left

    def two_rows_per_pass(self):
        calls["n"] += 1
        return real_left(self) if calls["n"] <= 2 else 0.0

    monkeypatch.setattr(ci_head_run._Deadline, "left", two_rows_per_pass)
    calls["n"] = 0
    await ci_head_run.announce_results(db, known, ci_head_run._Deadline(100))
    assert await journal.get_state(db, journal.ANNOUNCE_CURSOR) > 0, (
        "прервано посреди страницы: курсор стоит на последней обработанной строке"
    )
    # свежий отчёт возвращает очередь к вершине: он не ждёт хвоста
    fresh_branch = "task-21/fresh"
    cloud.heads[fresh_branch] = HEAD_B
    fresh = await _cloud_task(db, slug="freshrow", branch=fresh_branch)
    await journal.observe_head(
        db, fresh, HEAD_B, fresh_branch, grace_seconds=0, retry_seconds=0
    )
    await accept_ci_run_report(
        db,
        fresh,
        **cloud.github.attempt(
            fresh, HEAD_B, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="2301"
        ),
    )
    known[fresh] = HEAD_B
    calls["n"] = 0
    await ci_head_run.announce_results(db, known, ci_head_run._Deadline(100))
    assert len(await _announced(db, fresh)) == 1, "свежая попытка - с вершины"

    for _ in range(4):
        calls["n"] = 0
        await ci_head_run.announce_results(db, known, ci_head_run._Deadline(100))
    assert len(await _announced(db, good)) == 1, (
        "курсор шёл вниз, а не топтался на месте"
    )


async def test_finished_tasks_leave_the_journals(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I5: строки завершённых задач старше срока чистятся; живых - нет."""
    from hub import repository_ci_head as journal

    done = await _cloud_task(db, slug="finished", branch="task-22/done")
    cloud.heads["task-22/done"] = HEAD_A
    live = await _cloud_task(db, slug="alive", branch="task-22/live")
    cloud.heads["task-22/live"] = HEAD_B
    await _cycle(db)
    for tid in (done, live):
        await db.execute(
            "INSERT INTO ci_head_announced (task_id, sha, attempt_key) VALUES (?, 'x', 'k')",
            (tid,),
        )
    await db.execute(
        "UPDATE tasks SET status='completed', updated_at=datetime('now', '-40 days') "
        "WHERE id=?",
        (done,),
    )
    # живая задача, давно без записей: журнал нужен, пока она в работе
    await db.execute(
        "UPDATE tasks SET updated_at=datetime('now', '-40 days') WHERE id=?", (live,)
    )
    await db.commit()
    removed = await journal.prune_finished(db, days=14)
    assert removed >= 2
    for table in ("ci_head_runs", "ci_head_announced"):
        cur = await db.execute(f"SELECT task_id FROM {table}")  # noqa: S608
        left = {r[0] for r in await cur.fetchall()}
        assert done not in left and live in left


async def _attempts_of(db: aiosqlite.Connection, task_id: int) -> tuple[int, str]:
    cur = await db.execute(
        "SELECT attempts, state FROM ci_head_runs WHERE task_id=?", (task_id,)
    )
    row = await cur.fetchone()
    return int(row[0]), str(row[1])


async def test_only_a_started_request_spends_the_attempt(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I3 (Codex круг 3): форж отказал до POST, исключение или отмена до отправки - попытка цела."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run

    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    task_id = await _cloud_task(db)

    # форж отказал до отправки (список workflow не прочитан): три прохода подряд
    cloud.outcomes = [
        CIRunRequestResult(CIRunRequestOutcome.unavailable, "workflows_unreadable")
    ] * 3
    for _ in range(3):
        ci_head_run.reset_state()
        await _cycle(db)
    assert len(cloud.requests) == 3
    assert await _attempts_of(db, task_id) == (0, "seen"), "отказ до отправки не тратит"

    # настоящая отправка с неизвестным исходом тратит попытку
    cloud.outcomes = [
        CIRunRequestResult(CIRunRequestOutcome.unavailable, "dispatch_call_failed")
    ]
    ci_head_run.reset_state()
    await _cycle(db)
    assert (await _attempts_of(db, task_id))[0] == 1

    # исключение после резерва до отправки: попытка возвращается
    other = await _cloud_task(db, slug="boom", branch="task-23/boom")
    cloud.heads["task-23/boom"] = HEAD_B

    async def boom(*args, **kwargs):
        raise RuntimeError("чтение области упало")

    real = journal.task_in_scope
    monkeypatch.setattr(journal, "task_in_scope", boom)
    ci_head_run.reset_state()
    await _cycle(db)
    assert (await _attempts_of(db, other)) == (0, "seen")

    # отмена до отправки: то же
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(journal, "task_in_scope", real)
    cloud.open_pr_between = cancelled  # type: ignore[method-assign]
    ci_head_run.reset_state()
    with pytest.raises(asyncio.CancelledError):
        await _cycle(db)
    assert (await _attempts_of(db, other)) == (0, "seen")


async def test_the_overdue_alert_is_rechecked_inside_its_own_transaction(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """I2 (Codex круг 3): задачу сдали, пока читалась голова - alert не пишется."""
    from hub.services import ci_head_run

    task_id = await _requested_overdue(db, cloud, monkeypatch, "late", "task-24/late")
    ci_head_run.reset_state()

    reads_before = cloud.head_reads

    async def submitted_during_the_read():
        if cloud.head_reads < reads_before + 2:
            return  # первое чтение - половина запросов; сдача - на чтении просрочки
        await db.execute(
            "UPDATE tasks SET submission_generation=1, submission_sha='x' WHERE id=?",
            (task_id,),
        )
        await db.commit()

    cloud.on_heads = submitted_during_the_read
    await _cycle(db)
    assert await _notes(db, task_id, "CI не пришёл") == []


async def test_a_failed_top_save_loses_no_result_and_announces_nothing_twice(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """#1914: голова очереди идемпотентна - сбой записи вершины не теряет и не дублирует итог."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    cloud.heads["task-26/top"] = HEAD_A
    task_id = await _cloud_task(db, slug="toprow", branch="task-26/top")
    await _cycle(db)
    await accept_ci_run_report(
        db,
        task_id,
        **cloud.github.attempt(
            task_id, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="2601"
        ),
    )
    await db.execute(
        "CREATE TRIGGER boom BEFORE UPDATE ON ci_head_state "
        "WHEN NEW.key='announce_top' BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    await db.execute(
        "CREATE TRIGGER boom_ins BEFORE INSERT ON ci_head_state "
        "WHEN NEW.key='announce_top' BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    await db.commit()
    with pytest.raises(Exception, match="boom"):
        await ci_head_run.announce_results(db, {}, ci_head_run._Deadline(30))
    await db.rollback()
    assert await journal.get_state(db, journal.ANNOUNCE_TOP) == 0
    await db.execute("DROP TRIGGER boom")
    await db.execute("DROP TRIGGER boom_ins")
    await db.commit()
    await ci_head_run.announce_results(db, {}, ci_head_run._Deadline(30))
    assert len(await _announced(db, task_id)) == 1


# ---- #1914: пять граничных случаев третьего круга ревью #1862 ----


async def test_sql_of_the_pass_stays_inside_the_budget(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-1: 1001 пригодная задача, бюджет 20 мс - проход укладывается с допуском одной операции."""
    from hub.services import ci_head_run

    first = await _cloud_task(db, slug="many", branch="task-27/0")
    cur = await db.execute("PRAGMA table_info(tasks)")
    cols = [r[1] for r in await cur.fetchall() if r[1] not in ("id", "branch")]
    names = ", ".join(cols)
    for i in range(1, 1001):
        await db.execute(
            f"INSERT INTO tasks ({names}, branch) "  # noqa: S608
            f"SELECT {names}, ? FROM tasks WHERE id=?",
            (f"task-27/{i}", first),
        )
    await db.commit()
    cur = await db.execute("SELECT COUNT(*) FROM tasks WHERE status='running'")
    assert (await cur.fetchone())[0] == 1001
    monkeypatch.setattr(config, "CI_HEAD_RUN_TASKS_PER_CYCLE", 1000)
    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", 0.02)
    started = time.monotonic()
    await _cycle(db)
    elapsed = time.monotonic() - started
    assert elapsed < 0.02 + 0.1, f"проход {elapsed:.3f} с при бюджете 0.02 с"
    assert not db.in_transaction, "после прохода соединение поллера не в транзакции"


async def test_the_scan_walks_past_tasks_out_of_scope(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-1 (курсор): пачка задач вне области не запирает очередь, просмотр ограничен."""
    from hub.services import ci_head_run

    monkeypatch.setattr(config, "CI_HEAD_RUN_TASKS_PER_CYCLE", 2)
    for i in range(40):
        await _cloud_task(db, slug=f"off{i}", branch=f"task-28/off{i}", mode="off")
    cloud.heads["task-28/live"] = HEAD_B
    await _cloud_task(db, slug="live", branch="task-28/live")
    for _ in range(40):
        await _cycle(db)
        if cloud.requests:
            break
    assert [r[0] for r in cloud.requests] == ["task-28/live"]
    del ci_head_run


async def test_only_a_started_post_spends_the_attempt(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-2: настоящий GitHubForge, GET списка workflow дольше потолка - попытка цела."""
    from hub.services import ci_head_run

    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    monkeypatch.setattr(ci_head_run, "DISPATCH_CEILING_SECONDS", 0.2)
    posts: list[tuple[str, ...]] = []

    async def slow_get(*cmd, **kw):
        if "dispatches" in " ".join(cmd):
            posts.append(cmd)
            return (0, "", "")
        await asyncio.sleep(5)  # GET списка workflow
        return (0, "{}", "")

    monkeypatch.setattr("hub.integrations.proc.run", slow_get)
    forge = GitHubForge()

    async def real_request(
        branch, repo=None, gh_repo=None, forge="", workflow_path=None, on_send=None
    ):
        return await GitHubForge().request_ci_run(
            branch,
            repo=repo,
            gh_repo=gh_repo,
            workflow_path=workflow_path,
            on_send=on_send,
        )

    del forge
    cloud.request_ci_run = real_request  # type: ignore[method-assign]
    task_id = await _cloud_task(db)
    for _ in range(3):
        ci_head_run.reset_state()
        await _cycle(db)
    assert posts == []
    assert await _attempts_of(db, task_id) == (0, "seen"), (
        "GET дольше потолка ни разу не потратил попытку; exhausted не наступил"
    )

    # а таймаут уже ПОСЛЕ старта POST - потрачена
    async def slow_post(*cmd, **kw):
        if "dispatches" in " ".join(cmd):
            await asyncio.sleep(5)
            return (0, "", "")
        return (
            0,
            '{"workflows": [{"id": 1, "state": "active", "path": "%s"}]}' % SAT_PATH,
            "",
        )

    monkeypatch.setattr("hub.integrations.proc.run", slow_post)
    ci_head_run.reset_state()
    await _cycle(db)
    assert (await _attempts_of(db, task_id))[0] == 1, "POST начался - попытка потрачена"


async def test_overdue_alert_requires_the_same_scope_identity(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-3: ожидание для /old; во время чтения головы проект переключён на /new."""
    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 60, raising=False)
    task_id = await _cloud_task(db, slug="ident", branch="task-29/i", workspace="/old")
    cloud.heads["task-29/i"] = HEAD_A
    await _cycle(db)
    assert len(cloud.requests) == 1
    monkeypatch.setattr(config, "CI_HEAD_RUN_WAIT_MINUTES", 0)
    from hub.services import ci_head_run

    ci_head_run.reset_state()
    reads_before = cloud.head_reads

    async def switched_during_the_read():
        if cloud.head_reads < reads_before + 2:
            return  # первое чтение - половина запросов; переключение - на чтении просрочки
        project = await repo.resolve_project_for_task(db, task_id)
        await repo.update_project(db, project["id"], workspace_path="/new")
        await db.commit()

    cloud.on_heads = switched_during_the_read
    await _cycle(db)
    assert await _notes(db, task_id, "CI не пришёл") == [], (
        "голова прочитана из /old, а проект уже в /new: alert ложный"
    )


async def test_overdue_alert_is_not_written_once_the_result_is_announced(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-3 (состояние): итог объявлен, пока читали голову, - «CI не пришёл» не пишется."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run

    task_id = await _requested_overdue(db, cloud, monkeypatch, "annc", "task-30/a")
    ci_head_run.reset_state()
    reads_before = cloud.head_reads

    async def announced_during_the_read():
        if cloud.head_reads < reads_before + 2:
            return
        async with write_transaction(db):
            await journal.claim_announcement(db, task_id, HEAD_A, "9001:1")

    cloud.on_heads = announced_during_the_read
    await _cycle(db)
    assert await _notes(db, task_id, "CI не пришёл") == []


async def test_the_queue_tail_progresses_under_constant_inflow(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """AC-4: один пригодный старый итог за 201 непригодной строкой, новый посторонний отчёт перед каждым проходом."""
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    cloud.heads["task-31/good"] = HEAD_A
    good = await _cloud_task(db, slug="oldgood", branch="task-31/good")
    await _cycle(db)
    await accept_ci_run_report(
        db,
        good,
        **cloud.github.attempt(
            good, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="3101"
        ),
    )
    bad = await _cloud_task(db, slug="unfit", branch="task-31/bad")

    async def unfit_row(i: int) -> None:
        sha = f"{i + 1:040x}"
        await db.execute(
            "INSERT INTO ci_head_runs (task_id, sha, branch, state) "
            "VALUES (?, ?, 'task-31/bad', 'observed')",
            (bad, sha),
        )
        await db.execute(
            "INSERT INTO ci_report_attempts (task_id, head_sha, run_id, run_attempt, "
            "trust, trust_reason, trust_final, contract_fp) "
            "VALUES (?, ?, ?, '1', 'verified', '', 1, 'old-contract')",
            (bad, sha, str(4000 + i)),
        )

    for i in range(201):
        await unfit_row(i)
    await db.commit()
    known = {good: HEAD_A, bad: HEAD_A}
    for n in range(12):
        await unfit_row(1000 + n)  # посторонний отчёт перед каждым проходом
        await db.commit()
        await ci_head_run.announce_results(db, known, ci_head_run._Deadline(100))
        if await _announced(db, good):
            break
    assert len(await _announced(db, good)) == 1, (
        "приток чужих отчётов не должен возвращать хвост к вершине бессрочно"
    )
    assert await _announced(db, bad) == []


async def test_unarchived_task_keeps_its_idempotency(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """AC-5: архив на 40 суток, разархивация - ни повторных запросов CI, ни повторного объявления."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    monkeypatch.setattr(config, "CI_HEAD_RUN_RETRY_SECONDS", 0)
    asked = await _cloud_task(db, slug="asked", branch="task-32/asked")
    cloud.heads["task-32/asked"] = HEAD_A
    for _ in range(3):  # два запроса и закрытие exhausted
        await _cycle(db)
    assert len(cloud.requests) == 2
    told = await _cloud_task(db, slug="told", branch="task-32/told")
    cloud.heads["task-32/told"] = HEAD_B
    await _cycle(db)
    await accept_ci_run_report(
        db,
        told,
        **cloud.github.attempt(
            told, HEAD_B, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="3201"
        ),
    )
    await _cycle(db)
    assert len(await _announced(db, told)) == 1
    requests_before = len(cloud.requests)

    await db.execute(
        "UPDATE tasks SET archived=1, updated_at=datetime('now', '-40 days') "
        "WHERE id IN (?, ?)",
        (asked, told),
    )
    await db.commit()
    await journal.prune_finished(db, days=14)
    await db.execute("UPDATE tasks SET archived=0 WHERE id IN (?, ?)", (asked, told))
    await db.commit()
    for _ in range(3):
        ci_head_run.reset_state()
        await _cycle(db)
    assert len(cloud.requests) == requests_before, (
        "разархивированная задача не получает новых запросов CI на тот же sha"
    )
    assert len(await _announced(db, told)) == 1, "тот же run не объявлен повторно"


async def test_pruning_works_in_bounded_batches_of_whole_tasks(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """AC-1 (очистка): порция - целые задачи, обе таблицы; маркер не уходит раньше головы."""
    from hub import repository_ci_head as journal

    ids = []
    for i in range(3):
        task_id = await _cloud_task(db, slug=f"batch{i}", branch=f"task-33/b{i}")
        ids.append(task_id)
        for k in range(4):
            await db.execute(
                "INSERT INTO ci_head_runs (task_id, sha, branch) VALUES (?, ?, 'x')",
                (task_id, f"{k:040x}"),
            )
            await db.execute(
                "INSERT INTO ci_head_announced (task_id, sha, attempt_key) "
                "VALUES (?, ?, 'k')",
                (task_id, f"{k:040x}"),
            )
    await db.execute(
        "UPDATE tasks SET status='completed', updated_at=datetime('now', '-40 days')"
    )
    await db.commit()
    assert await journal.prune_finished(db, days=14, batch=2) == 16  # 2 задачи x 8
    for table in ("ci_head_runs", "ci_head_announced"):
        cur = await db.execute(
            f"SELECT task_id, COUNT(*) FROM {table} GROUP BY task_id"  # noqa: S608
        )
        left = {r[0]: r[1] for r in await cur.fetchall()}
        assert len(left) == 1 and list(left.values()) == [4], (
            f"{table}: остаётся целая задача, а не обрывок"
        )
    assert await journal.prune_finished(db, days=14, batch=2) == 8


async def test_an_interrupted_head_does_not_starve_the_tail(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """Codex P2-1: голова вышла по времени - хвост всё равно идёт, вершина не сдвигается."""
    from hub import repository_ci_head as journal
    from hub.services import ci_head_run
    from hub.services.ci_report import accept_ci_run_report

    cloud.heads["task-34/g"] = HEAD_A
    good = await _cloud_task(db, slug="tailgood", branch="task-34/g")
    await _cycle(db)
    await accept_ci_run_report(
        db,
        good,
        **cloud.github.attempt(
            good, HEAD_A, path=SAT_PATH, checks=dict(SAT_CHECKS), run_id="3401"
        ),
    )
    monkeypatch.setattr(ci_head_run, "HEAD_SHARE", 0.0)  # голове времени нет
    await ci_head_run.announce_results(db, {good: HEAD_A}, ci_head_run._Deadline(100))
    assert len(await _announced(db, good)) == 1, "хвост прошёл, хотя голова прервана"
    assert await journal.get_state(db, journal.ANNOUNCE_TOP) == 0, (
        "голова не дочитана: вершина не сдвинута, повтор в следующий проход"
    )


async def test_a_slow_scan_leaves_time_for_the_requests(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """Codex P2-3: просмотр кандидатов не съедает бюджет целиком - проход делает работу."""
    from hub.services import ci_head_run

    for i in range(20):
        branch = f"task-35/s{i}"
        cloud.heads[branch] = f"{i + 1:02x}" * 20
        await _cloud_task(db, slug=f"scan{i}", branch=branch)
    real = repo.resolve_project_for_task

    async def slow(conn, task_id):
        await asyncio.sleep(0.03)
        return await real(conn, task_id)

    monkeypatch.setattr(repo, "resolve_project_for_task", slow)
    monkeypatch.setattr(ci_head_run, "CYCLE_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr(ci_head_run, "MIN_DISPATCH_LEFT_SECONDS", 0.02)
    await _cycle(db)
    assert len(cloud.requests) >= 1, "медленный просмотр не оставил проход без запросов"


async def test_a_refusal_before_the_post_keeps_the_attempt_even_when_final(
    db: aiosqlite.Connection, cloud: _CloudGit
):
    """Codex P3-4: declined до POST (нет известного workflow) - итог, но попытка не потрачена."""
    cloud.outcomes = [
        CIRunRequestResult(CIRunRequestOutcome.declined, "no_known_ci_workflow")
    ]
    task_id = await _cloud_task(db, slug="declinedpre", branch="task-36/d")
    cloud.heads["task-36/d"] = HEAD_A
    await _cycle(db)
    assert await _attempts_of(db, task_id) == (0, "declined")


async def test_overdue_and_pruning_wait_for_the_budget_of_the_pass(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch
):
    """P2 (Codex круг 3): бюджет прохода вышел - просрочка не начинает сеть."""
    from hub.services import ci_head_run

    task_id = await _requested_overdue(db, cloud, monkeypatch, "budget", "task-25/b")
    reads = cloud.head_reads
    await ci_head_run.announce_overdue(db, ci_head_run._Deadline(0.0))
    assert cloud.head_reads == reads
    assert await _notes(db, task_id, "CI не пришёл") == []
    await ci_head_run.announce_overdue(db, ci_head_run._Deadline(30.0))
    assert len(await _notes(db, task_id, "CI не пришёл")) == 1


@pytest.mark.parametrize("kind", ["submitted", "policy_off"])
async def test_unfit_overdue_rows_do_not_hide_the_legit_one(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch, kind: str
):
    """Вердикт 1, P2: 20 непригодных ожиданий + 1 законное - alert приходит.

    ``submitted`` отсекает SQL; ``policy_off`` - Python, там нужен круговой курсор.
    """
    tasks = []
    for i in range(21):
        tasks.append(
            await _requested_overdue(
                db, cloud, monkeypatch, f"{kind}{i}", f"t37/{kind}{i}"
            )
        )
    for task_id in tasks[:20]:
        if kind == "submitted":
            await db.execute(
                "UPDATE tasks SET submission_generation=1, submission_sha='x' "
                "WHERE id=?",
                (task_id,),
            )
        else:
            project = await repo.resolve_project_for_task(db, task_id)
            await repo.update_project(
                db,
                project["id"],
                gate_policy=json.dumps(
                    {"ci_contract": SAT_CONTRACT, "ci_before_submit": "off"}
                ),
            )
    await db.commit()
    for _ in range(4):  # без reset_state: круговой курсор живёт между проходами
        await _cycle(db)
        if await _notes(db, tasks[20], "CI не пришёл"):
            break
    assert len(await _notes(db, tasks[20], "CI не пришёл")) == 1


@pytest.mark.parametrize(
    "change",
    [
        "submission_generation=1, submission_sha='x'",
        "branch='task-38/other'",
        "pr_number=7",
    ],
)
async def test_overdue_window_is_filtered_by_task_scope_in_sql(
    db: aiosqlite.Connection, cloud: _CloudGit, monkeypatch, change: str
):
    """Вердикт 1, P2: условия области задачи стоят в SQL просрочек, а не в Python."""
    from hub import repository_ci_head as journal

    task_id = await _requested_overdue(db, cloud, monkeypatch, "sqlscope", "task-38/s")
    assert len(await journal.overdue_waits(db, 0)) == 1
    await db.execute(f"UPDATE tasks SET {change} WHERE id=?", (task_id,))  # noqa: S608
    await db.commit()
    assert await journal.overdue_waits(db, 0) == []
