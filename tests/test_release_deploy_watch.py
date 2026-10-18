"""Релиз влит, а в проде его нет дольше срока — уведомление стюарду (#1828).

Хаб умел вычислять «в проде / не в проде» только по запросу (#497, #499), и
пропуск деплоя 17.10 (#1796) заметил человек. Теперь факт релизного мержа
хранится в ``release_merges``, а свип поллера проверяет достижимость
КОНКРЕТНОГО ``release_sha`` от последнего успешного выката.

Три ветки, и ни одна не выдаёт незнание за отрицание: достижим — молчание;
недостижим при известном выкате — ``release_not_in_prod``; нет данных о выкате
— ``release_deploy_unobserved``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import release_deploy_watch as watch
from hub.services.release import merge_ready_release
from tests.test_release import _git, _project

HUB_REPO = "agentdrover/haiplane"
RELEASE_A = "a" * 40
RELEASE_B = "b" * 40
DEPLOYED = "d" * 40
DEPLOYED_LATER = "e" * 40
OLD = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture(autouse=True)
def _hub_repo():
    with patch.object(config, "REPO_NAME", HUB_REPO):
        yield


def _ancestry(answers: dict[tuple[str, str], bool | None]):
    """Git, который отвечает на is_ancestor по таблице; незаявленный вопрос — None."""
    g = _git()
    g.commit_exists = AsyncMock(return_value=True)
    g.fetch_commit = AsyncMock(return_value=(True, ""))

    async def is_ancestor(_repo, ancestor, descendant):
        return answers.get((ancestor, descendant))

    g.is_ancestor = AsyncMock(side_effect=is_ancestor)
    plugins.git_ops = g
    return g


async def _pid(db: aiosqlite.Connection) -> int:
    return int(dict(await _project(db))["id"])


async def _row(
    db: aiosqlite.Connection,
    pid: int,
    pr: int,
    sha: str | None,
    merged_at: str = OLD,
) -> int:
    cur = await db.execute(
        "INSERT INTO release_merges (project_id, release_pr, release_sha, merged_at) "
        "VALUES (?, ?, ?, ?)",
        (pid, pr, sha, merged_at),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def _events(db: aiosqlite.Connection, kind: str) -> list[dict]:
    rows = await repo.fetchall(
        db, "SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,)
    )
    return [dict(r) for r in rows]


async def _messages(db: aiosqlite.Connection) -> list[dict]:
    rows = await repo.fetchall(
        db,
        "SELECT body FROM agent_messages WHERE to_ref = 'steward@shipper' ORDER BY id",
    )
    return [json.loads(dict(r)["body"]) for r in rows]


async def _deploy(db: aiosqlite.Connection, pid: int, sha: str) -> None:
    await repo.record_release(db, deployed_sha=sha, project_id=pid, ref="main")


@pytest.mark.asyncio
async def test_a_release_that_did_not_reach_prod_is_reported_once(db) -> None:
    # AC-1. Релиз R старше срока, выкат D его не содержит.
    pid = await _pid(db)
    row_id = await _row(db, pid, 83, RELEASE_A)
    await _deploy(db, pid, DEPLOYED)
    _ancestry({(RELEASE_A, DEPLOYED): False, (RELEASE_A, DEPLOYED_LATER): True})

    await watch.sweep(db)

    events = await _events(db, "release_not_in_prod")
    assert len(events) == 1, events
    assert json.loads(events[0]["payload"])["release_merge_id"] == row_id
    messages = await _messages(db)
    assert len(messages) == 1 and messages[0]["reason"] == "release", messages

    await watch.sweep(db)  # повтор — без повтора
    assert len(await _events(db, "release_not_in_prod")) == 1
    assert len(await _messages(db)) == 1

    await _deploy(db, pid, DEPLOYED_LATER)  # потомок релиза доехал
    await watch.sweep(db)
    await watch.sweep(db)

    assert len(await _events(db, "release_deployed_late")) == 1
    assert len(await _messages(db)) == 1, "поздний выкат — без новой тревоги"


async def _scenario(db: aiosqlite.Connection) -> tuple[dict[str, int], int]:
    """Прогон свипа: сколько событий каждой ветки появилось и сколько сообщений."""
    kinds = (
        "release_not_in_prod",
        "release_deploy_unobserved",
        "release_deployed_late",
    )
    before = {k: len(await _events(db, k)) for k in kinds}
    before_msgs = len(await _messages(db))
    await watch.sweep(db)
    after = {k: len(await _events(db, k)) for k in kinds}
    return (
        {k: after[k] - before[k] for k in kinds},
        len(await _messages(db)) - before_msgs,
    )


@pytest.mark.asyncio
async def test_descendant_unknown_on_time_muted_and_legacy_cases(db) -> None:
    # AC-2.
    pid = await _pid(db)
    git = _ancestry({})

    # (б) ни одного успешного выката: «нет данных», не «не выкатилось».
    await _row(db, pid, 1, RELEASE_A)
    events, messages = await _scenario(db)
    assert events == {
        "release_not_in_prod": 0,
        "release_deploy_unobserved": 1,
        "release_deployed_late": 0,
    }
    assert messages == 1
    events, messages = await _scenario(db)  # повтор молчит
    assert (sum(events.values()), messages) == (0, 0)

    # (а) выкат потомка, отдельной строки для sha релиза нет — молчание.
    await db.execute("DELETE FROM release_merges")
    await db.commit()
    await _deploy(db, pid, DEPLOYED)
    _ancestry({(RELEASE_B, DEPLOYED): True})
    await _row(db, pid, 2, RELEASE_B)
    events, messages = await _scenario(db)
    assert (sum(events.values()), messages) == (0, 0)

    # (в) выкат в пределах срока: строка моложе срока в свип не попадает.
    await db.execute("DELETE FROM release_merges")
    await db.commit()
    git = _ancestry({(RELEASE_A, DEPLOYED): False})
    await _row(db, pid, 3, RELEASE_A, merged_at="2999-01-01 00:00:00")
    events, messages = await _scenario(db)
    assert (sum(events.values()), messages) == (0, 0)
    git.is_ancestor.assert_not_awaited()

    # (д) релиз до выката задачи: строки release_merges нет, только штамп
    # pipeline_merges — свип его не видит.
    await db.execute("DELETE FROM release_merges")
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha, "
        "released_pr, released_sha) VALUES (?, 4, NULL, ?, 4, ?)",
        (pid, "9" * 40, RELEASE_A),
    )
    await db.commit()
    events, messages = await _scenario(db)
    assert (sum(events.values()), messages) == (0, 0)
    git.is_ancestor.assert_not_awaited()

    # (г) повод release выключен политикой: событие есть, сообщения нет.
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"release": "auto", "steward_watch": ["stale"]})
    )
    await db.commit()
    await _row(db, pid, 5, RELEASE_A)
    events, messages = await _scenario(db)
    assert events["release_not_in_prod"] == 1
    assert messages == 0


@pytest.mark.asyncio
async def test_releases_are_watched_by_their_own_row(db) -> None:
    # AC-3.
    pid = await _pid(db)
    await _deploy(db, pid, DEPLOYED)

    # (е) работа задачи смержена позже и получила другой штамп: проверяется
    # release_sha строки, а не штамп pipeline_merges.
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha, "
        "released_pr, released_sha) VALUES (?, 9, NULL, ?, 9, ?)",
        (pid, "8" * 40, RELEASE_B),
    )
    await _row(db, pid, 6, RELEASE_A)
    git = _ancestry({(RELEASE_A, DEPLOYED): True})
    await watch.sweep(db)
    asked = [call.args[1:] for call in git.is_ancestor.await_args_list]
    assert asked == [(RELEASE_A, DEPLOYED)], asked
    assert not await _messages(db)

    # (з) git не ответил — «нет данных», одно сообщение.
    await db.execute("DELETE FROM release_merges")
    await db.commit()
    _ancestry({})
    await _row(db, pid, 7, RELEASE_A)
    await watch.sweep(db)
    await watch.sweep(db)
    assert len(await _events(db, "release_deploy_unobserved")) == 1
    assert len(await _messages(db)) == 1

    # (ж)+(и) настоящий релиз без строк pipeline_merges; первое чтение sha
    # пустое. Строка записана ДО чтения, штамп задач не выполнен.
    await db.execute("DELETE FROM release_merges")
    await db.execute("DELETE FROM pipeline_merges")
    await db.commit()
    g = _git()
    seen_at_read: list[int] = []

    async def merge_commit_sha(_pr, **_kw):
        rows = await repo.fetchall(db, "SELECT COUNT(*) AS n FROM release_merges")
        seen_at_read.append(int(dict(rows[0])["n"]))
        return ""

    g.merge_commit_sha = AsyncMock(side_effect=merge_commit_sha)
    g.commit_exists = AsyncMock(return_value=True)
    g.fetch_commit = AsyncMock(return_value=(True, ""))
    g.is_ancestor = AsyncMock(return_value=True)
    project = await repo.get_project(db, pid)
    with patch.object(repo, "mark_merges_released", AsyncMock()) as stamp:
        merged, reason = await merge_ready_release(db, project)
        assert merged is True, reason
        assert seen_at_read == [1], "строка релиза пишется до чтения sha"
        rows = await repo.fetchall(db, "SELECT * FROM release_merges")
        assert [dict(r)["release_sha"] for r in rows] == [None]
        await db.execute("UPDATE release_merges SET merged_at = ?", (OLD,))
        await db.commit()

        g.merge_commit_sha = AsyncMock(return_value=RELEASE_B)  # следующий свип прочёл
        await watch.sweep(db)
        stamp.assert_not_awaited()
    rows = await repo.fetchall(db, "SELECT release_sha FROM release_merges")
    assert [dict(r)["release_sha"] for r in rows] == [RELEASE_B]

    # Два релиза без sha старше срока — по одному сообщению каждому.
    await db.execute("DELETE FROM release_merges")
    await db.commit()
    plugins.git_ops.merge_commit_sha = AsyncMock(return_value="")
    before = len(await _messages(db))
    await _row(db, pid, 11, None)
    await _row(db, pid, 12, None)
    await watch.sweep(db)
    assert len(await _messages(db)) - before == 2
    await watch.sweep(db)
    assert len(await _messages(db)) - before == 2
    unobserved = await _events(db, "release_deploy_unobserved")
    assert any(
        "sha релиза не прочитан" in json.loads(e["payload"])["reason"]
        for e in unobserved
    )


@pytest.mark.asyncio
async def test_a_project_of_a_foreign_repository_is_not_watched(db) -> None:
    # Выкат хаб пишет только для собственного репозитория: чужой проект не
    # получает ни «не в проде», ни «нет данных» — для него это был бы вымысел.
    pid = await _pid(db)
    await repo.update_project(db, pid, repo="someone/else")
    await db.commit()
    await _row(db, pid, 1, RELEASE_A)
    git = _ancestry({(RELEASE_A, DEPLOYED): False})
    await _deploy(db, pid, DEPLOYED)

    await watch.sweep(db)

    assert not await _events(db, "release_not_in_prod")
    assert not await _events(db, "release_deploy_unobserved")
    git.is_ancestor.assert_not_awaited()


@pytest.fixture(autouse=True)
def _fresh_fetch_cache():
    watch._fetch_failed.clear()
    yield
    watch._fetch_failed.clear()


@pytest.mark.asyncio
async def test_unreachable_commits_are_fetched_once_per_backoff(db) -> None:
    # Недоступный git: два прохода и две строки — не больше одного fetch на sha.
    pid = await _pid(db)
    await _deploy(db, pid, DEPLOYED)
    await _row(db, pid, 1, RELEASE_A)
    await _row(db, pid, 2, RELEASE_B)
    g = _ancestry({})
    g.commit_exists = AsyncMock(return_value=False)
    g.fetch_commit = AsyncMock(return_value=(False, "timeout"))

    await watch.sweep(db)
    await watch.sweep(db)

    fetched = [c.args[1] for c in g.fetch_commit.await_args_list]
    assert sorted(fetched) == sorted({RELEASE_A, RELEASE_B, DEPLOYED}), fetched

    clock = watch._clock()
    with patch.object(watch, "_clock", lambda: clock + watch.FETCH_RETRY_SECONDS + 1):
        await watch.sweep(db)
    assert len(g.fetch_commit.await_args_list) == 6, "после срока — повтор"


@pytest.mark.asyncio
async def test_equal_sha_is_reachable_without_a_workspace(db) -> None:
    # Равенство sha решает вопрос без git: пустая копия не даёт «нет данных».
    pid = await _pid(db)
    await repo.update_project(db, pid, workspace_path="")
    await db.commit()
    await _deploy(db, pid, DEPLOYED)
    await _row(db, pid, 1, DEPLOYED)
    _ancestry({})

    await watch.sweep(db)
    assert not await _events(db, "release_deploy_unobserved")
    assert not await _messages(db)

    # После прежней тревоги — release_deployed_late.
    await db.execute(
        "INSERT INTO events (kind, project_id, actor, payload) VALUES "
        "('release_deploy_unobserved', ?, 'hub', json_object('release_merge_id', "
        "(SELECT id FROM release_merges)))",
        (pid,),
    )
    await db.commit()
    await watch.sweep(db)
    assert len(await _events(db, "release_deployed_late")) == 1


@pytest.mark.asyncio
async def test_same_name_on_another_forge_is_not_watched(db) -> None:
    pid = await _pid(db)
    await repo.update_project(db, pid, forge="gitverse")
    await db.commit()
    await _row(db, pid, 1, RELEASE_A)
    git = _ancestry({(RELEASE_A, DEPLOYED): False})
    await _deploy(db, pid, DEPLOYED)

    await watch.sweep(db)

    assert not await _events(db, "release_not_in_prod")
    git.is_ancestor.assert_not_awaited()
