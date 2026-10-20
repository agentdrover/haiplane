"""Предохранитель автономии стюарда: откат и красная база (#1865).

Сеть (git, форж) подменена двойником: история задана руками, прогоны CI заданы
по sha. Всё остальное - настоящие таблицы: реестр доставок ``pipeline_merges``,
лента событий, ``steward_false_approvals``, режим ``effective_mode``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.db import _migrate, fetchall
from hub.integrations.registry import plugins
from hub.services import dashboard, effective_policy, red_base, steward_breaker
from hub.services.steward_exit import (
    clear_false_approval,
    false_approve_events,
    sticky_false_approvals,
)
from hub.services.steward_shadow import effective_mode
from tests.test_steward_shadow import _clean_act_sample, _project, _task

CI = ".github/workflows/ci.yml"
OTHER = ".github/workflows/other.yml"
_PR = itertools.count(500)


def sha_of(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # nosec B324 - test ids


class FakeGit:
    """Двойник git и форжа: история, прогоны CI по sha, сбои по требованию."""

    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.runs: dict[str, list[dict[str, Any]] | None] = {}
        self.branch_runs: list[dict[str, Any]] = []
        self.fetch_ok = True
        self.facts_ok = True

    def commit(self, label: str, message: str = "", parent: str | None = None) -> str:
        sha = sha_of(label)
        parents = (
            [parent] if parent else ([self.history[-1]["sha"]] if self.history else [])
        )
        self.history.append({"sha": sha, "parents": parents, "message": message})
        return sha

    async def fetch_base(self, repo_path: str, base: str):
        return (self.fetch_ok, "" if self.fetch_ok else "network down")

    async def first_parent_after(
        self, repo_path: str, base: str, since: str, limit: int
    ):
        shas = [c["sha"] for c in self.history]
        if since and since not in shas:
            return {"head": shas[-1], "reset": True, "shas": []}
        start = shas.index(since) + 1 if since else len(shas)
        return {"head": shas[-1], "reset": False, "shas": shas[start:][:limit]}

    async def commit_facts(self, repo_path: str, shas: list[str]):
        if not self.facts_ok:
            return None
        by_sha = {c["sha"]: c for c in self.history}
        return {s: by_sha[s] for s in shas if s in by_sha}

    async def ci_runs_for_sha(
        self, sha, repo=None, gh_repo=None, forge="", workflow_path=CI
    ):
        return self.runs.get(sha)

    async def branch_ci_runs(self, branch, repo=None, gh_repo=None, forge=""):
        return self.branch_runs

    async def release_range(self, base, head, repo=None, gh_repo=None, forge=""):
        return []


@pytest.fixture
def git(monkeypatch) -> FakeGit:
    fake = FakeGit()
    monkeypatch.setattr(plugins, "git_ops", fake)
    return fake


def ci_run(conclusion: str, path: str = CI + "@refs/heads/develop") -> dict[str, Any]:
    return {
        "status": "completed",
        "conclusion": conclusion,
        "event": "push",
        "run_started_at": "2026-10-20T12:00:00Z",
        "path": path,
    }


def branch_run(sha: str, conclusion: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": "2026-10-20T12:00:00Z",
        "name": "CI",
    }


async def project_with_workspace(db: aiosqlite.Connection, slug: str) -> dict[str, Any]:
    project_id = await _project(db, slug)
    await db.execute(
        "UPDATE projects SET workspace_path='/srv/w', default_branch='develop' WHERE id=?",
        (project_id,),
    )
    await db.commit()
    return dict(await repo.get_project(db, project_id))  # type: ignore[arg-type]


STEWARD = ("steward", "approved", "steward_applied")


async def delivered(
    db: aiosqlite.Connection,
    project: dict[str, Any],
    merge_sha: str,
    verdicts: list[tuple[str, str, str | None, int]] | list[tuple[Any, ...]],
) -> int:
    """Задача, влитая коммитом ``merge_sha``; вердикты - события ленты до слияния.

    Каждый вердикт: (актор, вердикт, метка источника или None, поколение).
    """
    task_id = await _task(db, int(project["id"]))
    for i, (actor, verdict, source, generation) in enumerate(verdicts):
        payload: dict[str, Any] = {
            "verdict": verdict,
            "submission_generation": generation,
        }
        if source:
            payload["source"] = source
        await db.execute(
            "INSERT INTO events (kind, task_id, actor, payload, created_at) "
            "VALUES ('review_verdict_recorded', ?, ?, ?, ?)",
            (task_id, actor, json.dumps(payload), f"2026-10-20 11:0{i}:00"),
        )
    await db.commit()
    await repo.record_pipeline_merge(
        db,
        pr_number=next(_PR),
        merge_sha=merge_sha,
        project_id=int(project["id"]),
        task_id=task_id,
        merged_at="2026-10-20 12:00:00",
    )
    return task_id


def steward_approval(generation: int = 2) -> list[tuple[Any, ...]]:
    return [("steward", "approved", "steward_applied", generation)]


async def cases(db: aiosqlite.Connection) -> list[tuple[int, str, str]]:
    rows = await fetchall(
        db,
        "SELECT task_id, source, ref FROM steward_false_approvals "
        "WHERE source IN ('reverted', 'base_red_after_merge') ORDER BY id",
    )
    return [(int(r["task_id"]), str(r["source"]), str(r["ref"])) for r in rows]


async def count(db: aiosqlite.Connection, sql: str, *params: Any) -> int:
    return int((await fetchall(db, sql, params))[0][0])


async def notices(db: aiosqlite.Connection, task_id: int) -> tuple[int, int]:
    """(события предохранителя, alert в ленте задачи) по задаче."""
    events = await count(
        db,
        "SELECT COUNT(*) FROM events WHERE kind=? AND task_id=?",
        steward_breaker.EVENT_TRIPPED,
        task_id,
    )
    alerts = await count(
        db,
        "SELECT COUNT(*) FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE 'Предохранитель стюарда сработал%'",
        task_id,
    )
    return events, alerts


async def cursor(db: aiosqlite.Connection, project_id: int) -> str:
    return await steward_breaker._cursor_of(db, project_id)


def revert_message(target: str, subject: str = "x") -> str:
    return f'Revert "{subject}"\n\nThis reverts commit {target}, reversing changes.\n'


# --------------------------------------------------------------------------
# AC-1: откат влитого коммита
# --------------------------------------------------------------------------


async def test_revert_of_steward_approved_merge_trips_breaker(
    db: aiosqlite.Connection, git: FakeGit, monkeypatch
):
    """Откат доставки, одобренной стюардом (steward_applied), - один случай и одно уведомление.

    Не стюард: поколение 1 одобрено стюардом, доставлено поколение 2, одобренное
    человеком; актор не steward при метке; откат без строки «This reverts commit»
    в теле. Падение до коммита откатывает строку, уведомление и курсор вместе.
    """
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project = await project_with_workspace(db, "brk-revert")
    pid = int(project["id"])
    await _clean_act_sample(db, pid)
    assert await effective_mode(db) == "act", "предусловие: до отката act выдан"

    m_a, m_b, m_c, m_d, m_e, m_f = (sha_of(f"merge-{n}") for n in "abcdef")
    task_a = await delivered(db, project, m_a, steward_approval())
    task_b = await delivered(
        db,
        project,
        m_b,
        [("steward", "approved", "steward_applied", 1), ("denis", "approved", None, 2)],
    )
    task_c = await delivered(db, project, m_c, steward_approval())
    task_d = await delivered(db, project, m_d, [("denis", "approved", None, 2)])
    task_e = await delivered(
        db, project, m_e, [("pda_claude", "approved", "steward_applied", 2)]
    )
    # Имя актора steward без метки применения: так выглядит подделка через тело запроса.
    task_f = await delivered(db, project, m_f, [("steward", "approved", None, 2)])

    git.commit("base-0", "chore: start")
    assert await steward_breaker.collect_reverts(db, project) == 0
    head = git.history[-1]["sha"]
    assert await cursor(db, pid) == head, "первый тик ставит курсор и ничего не судит"

    r_a = git.commit("revert-a", revert_message(m_a))
    git.commit("revert-b", revert_message(m_b))
    # Без строки «This reverts commit» в теле: тема и упоминание sha - не откат.
    git.commit("revert-c", f'Revert "feat: c"\n\nОткат вручную, см. {m_c}.\n')
    git.commit("revert-d", revert_message(m_d))
    git.commit("revert-e", revert_message(m_e))
    git.commit("revert-f", revert_message(m_f))
    git.commit("revert-unknown", revert_message(sha_of("never-merged")))

    # Падение после записи случая, до коммита: нет ни строки, ни уведомления.
    real_set_cursor = steward_breaker._set_cursor

    async def crash(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("process died")

    monkeypatch.setattr(steward_breaker, "_set_cursor", crash)
    assert await steward_breaker.collect_reverts(db, project) == 0
    monkeypatch.setattr(steward_breaker, "_set_cursor", real_set_cursor)
    assert await cases(db) == []
    assert await notices(db, task_a) == (0, 0)
    assert await cursor(db, pid) == head, "курсор не двинулся"

    assert await steward_breaker.collect_reverts(db, project) == 1
    assert await cases(db) == [(task_a, "reverted", r_a)]
    assert await effective_mode(db) == "shadow"
    assert await notices(db, task_a) == (1, 1)
    for other in (task_b, task_c, task_d, task_e, task_f):
        assert await notices(db, other) == (0, 0)
    assert await cursor(db, pid) == git.history[-1]["sha"]

    # Второй тик и повторный разбор тех же коммитов (курсор откатили назад):
    # ни второй строки, ни второго уведомления.
    assert await steward_breaker.collect_reverts(db, project) == 0
    await db.execute(
        "UPDATE steward_breaker_cursor SET sha=? WHERE project_id=?", (head, pid)
    )
    await db.commit()
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await cases(db) == [(task_a, "reverted", r_a)]
    assert await notices(db, task_a) == (1, 1)

    banner = await steward_breaker.banner_rows(db)
    assert [(b["task_id"], b["source"]) for b in banner] == [(task_a, "reverted")]
    inbox = await dashboard.get_inbox_data(db)
    assert inbox["steward_breaker_banner"] == banner, "видно и без steward_watch"
    quiet = {**inbox, "steward_breaker_banner": []}
    assert dashboard.inbox_attention_total(quiet) == inbox["inbox_attention_total"] - 1


async def test_revert_collector_judges_nothing_it_cannot_prove(
    db: aiosqlite.Connection, git: FakeGit, monkeypatch
):
    """Недоступный git, переписанная история и лимит за тик: ничего не обвиняется зря."""
    project = await project_with_workspace(db, "brk-unknown")
    pid = int(project["id"])
    m_x = sha_of("merge-x")
    task_x = await delivered(db, project, m_x, steward_approval())
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    start = await cursor(db, pid)
    git.commit("revert-x", revert_message(m_x))

    git.fetch_ok = False
    assert await steward_breaker.collect_reverts(db, project) == 0
    git.fetch_ok, git.facts_ok = True, False
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await cursor(db, pid) == start, "не ответили - курсор стоит"
    assert await cases(db) == []

    git.facts_ok = True
    await db.execute(
        "UPDATE steward_breaker_cursor SET sha=? WHERE project_id=?", ("0" * 40, pid)
    )
    await db.commit()
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await cursor(db, pid) == git.history[-1]["sha"], (
        "переписанная история: сброс"
    )
    assert await cases(db) == [], "история под курсором не судится"

    # Лимит: за тик берётся самый старый коммит после курсора, курсор идёт по одному.
    monkeypatch.setattr(steward_breaker, "REVERT_BATCH", 1)
    await db.execute(
        "UPDATE steward_breaker_cursor SET sha=? WHERE project_id=?", (start, pid)
    )
    await db.commit()
    git.commit("noise", "chore: noise")
    assert await steward_breaker.collect_reverts(db, project) == 1
    assert await cursor(db, pid) == git.history[1]["sha"], "ушёл только revert-x"
    assert await notices(db, task_x) == (1, 1)
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await cursor(db, pid) == git.history[2]["sha"]


async def test_a_revert_names_a_delivery_by_a_unique_prefix_only(
    db: aiosqlite.Connection, git: FakeGit
):
    project = await project_with_workspace(db, "brk-prefix")
    m_one = "ab" * 6 + "1" * 28
    m_two = "ab" * 6 + "2" * 28
    await delivered(db, project, m_one, steward_approval())
    await delivered(db, project, m_two, steward_approval())
    m_solo = sha_of("solo")
    task_solo = await delivered(db, project, m_solo, steward_approval())
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    git.commit("short-ambiguous", f"This reverts commit {'ab' * 6}")
    git.commit("short-too-short", f"This reverts commit {m_solo[:8]}")
    r_ok = git.commit("short-unique", f"This reverts commit {m_solo[:12]}")
    assert await steward_breaker.collect_reverts(db, project) == 1
    assert await cases(db) == [(task_solo, "reverted", r_ok)]


# --------------------------------------------------------------------------
# AC-2: красная база сразу после доставки стюарда
# --------------------------------------------------------------------------


async def test_base_red_after_steward_merge_trips_breaker(
    db: aiosqlite.Connection, git: FakeGit, monkeypatch
):
    """История G->M->B: случай только если red_sha - сама доставка при зелёном родителе."""
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    project = await project_with_workspace(db, "brk-red")
    await _clean_act_sample(db, int(project["id"]))
    project = dict(await repo.get_project(db, int(project["id"])))  # type: ignore[arg-type]

    g = git.commit("g", "chore: green base")
    m = git.commit("m", "feat: delivered by steward")
    b = git.commit("b", "feat: someone else")
    task_m = await delivered(db, project, m, steward_approval())
    git.runs[g] = [ci_run("success")]
    git.runs[m] = [ci_run("failure")]
    git.runs[b] = [ci_run("failure")]

    async def tick(*runs: dict[str, Any]) -> None:
        git.branch_runs = list(runs)
        await red_base.check_project(db, await repo.get_project(db, int(project["id"])))

    # (б) красный b - не доставка стюарда: красный m обвинения не даёт.
    await tick(branch_run(b, "failure"))
    assert await cases(db) == []
    assert await effective_mode(db) == "act"

    # (в) у родителя нет результата CI (ответ форджа пуст или недоступен).
    g2 = git.commit("g2", "chore: parent without runs")
    m2 = git.commit("m2", "feat: second delivery")
    task_m2 = await delivered(db, project, m2, steward_approval())
    git.runs[m2] = [ci_run("failure")]
    git.runs[g2] = []
    await tick(branch_run(m2, "failure"))
    git.runs[g2] = None
    await tick(branch_run(m2, "failure"))
    assert await cases(db) == []

    # Прогоны других workflow не считаются ни красными, ни зелёными.
    g3 = git.commit("g3", "chore: parent")
    m3 = git.commit("m3", "feat: third delivery")
    await delivered(db, project, m3, steward_approval())
    git.runs[g3] = [ci_run("success", OTHER + "@refs/heads/develop")]
    git.runs[m3] = [ci_run("failure", OTHER + "@refs/heads/develop")]
    await tick(branch_run(m3, "failure"))
    git.runs[g3] = [ci_run("success")]
    await tick(branch_run(m3, "failure"))
    assert await cases(db) == []
    # Родитель красный: база была сломана до доставки.
    git.runs[g2] = [ci_run("failure")]
    await tick(branch_run(m2, "failure"))
    assert await cases(db) == []

    # (а) красный m при зелёном родителе g: случай, и стюард в тени.
    await tick(branch_run(m, "failure"), branch_run(g, "success"))
    assert await cases(db) == [(task_m, "base_red_after_merge", m)]
    assert await effective_mode(db) == "shadow"
    assert await notices(db, task_m) == (1, 1)

    # Повторные тики при той же красной базе: ни второй строки, ни уведомления.
    await tick(branch_run(m, "failure"), branch_run(g, "success"))
    await tick(branch_run(m, "failure"), branch_run(g, "success"))
    assert await cases(db) == [(task_m, "base_red_after_merge", m)]
    assert await notices(db, task_m) == (1, 1)
    assert await notices(db, task_m2) == (0, 0)


# --------------------------------------------------------------------------
# AC-3: снятие и сводка
# --------------------------------------------------------------------------


async def test_new_sources_clear_and_report_like_existing(
    db: aiosqlite.Connection, git: FakeGit, client
):
    """Два отката одного коммита; снятие первого не гасит второй; счётчик за 14 дней."""
    project = await project_with_workspace(db, "brk-clear")
    pid = int(project["id"])
    m = sha_of("merge-t")
    task_t = await delivered(db, project, m, steward_approval())
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)

    r1 = git.commit("revert-1", revert_message(m, "first"))
    await steward_breaker.collect_reverts(db, project)
    assert await clear_false_approval(db, task_t, "denis", "разобрано") == 1
    assert await sticky_false_approvals(db) == []

    r2 = git.commit("revert-2", revert_message(m, "second"))
    await steward_breaker.collect_reverts(db, project)
    active = [(f.task_id, f.source, f.ref) for f in await sticky_false_approvals(db)]
    assert active == [(task_t, "reverted", r2)], "второй случай остаётся активным"
    assert [c[2] for c in await cases(db)] == [r1, r2]
    assert [b["ref"] for b in await steward_breaker.banner_rows(db)] == [r2]

    # Случай старше окна в счёт не входит; снятый входит.
    await db.execute(
        "INSERT INTO steward_false_approvals (task_id, source, ref, detected_at) "
        "VALUES (?, 'reverted', 'old', datetime('now', '-20 days'))",
        (task_t,),
    )
    await db.commit()
    assert await false_approve_events(db) == {"reverted": 2}

    data = (await client.get("/api/projects/brk-clear/effective-policy")).json()
    assert data["steward"]["contour"]["false_approve_events_14d"] == {"reverted": 2}
    text = "\n".join(effective_policy.format_effective_policy(data))
    assert "false_approve_events_14d: reverted 2" in text
    assert pid  # проект тот же, что у случаев


# --------------------------------------------------------------------------
# Миграция (R3): свежая установка и повторное применение
# --------------------------------------------------------------------------


async def test_cursor_migration_fresh_install_and_reapply(db: aiosqlite.Connection):
    columns = [
        r["name"]
        for r in await fetchall(db, "PRAGMA table_info(steward_breaker_cursor)")
    ]
    assert columns == ["project_id", "sha", "updated_at"], "свежая установка"
    await steward_breaker._set_cursor(db, 7, "a" * 40)
    await db.commit()

    # Повтор: шаг числится непримённым, таблица уже есть - данные целы.
    name = "create_steward_breaker_cursor"
    await db.execute("DELETE FROM _migrations WHERE name=?", (name,))
    await db.commit()
    await _migrate(db)
    await _migrate(db)
    assert await steward_breaker._cursor_of(db, 7) == "a" * 40
    assert await count(db, "SELECT COUNT(*) FROM _migrations WHERE name=?", name) == 1

    # Обновление с базы без таблицы: шаг создаёт её.
    await db.execute("DROP TABLE steward_breaker_cursor")
    await db.execute("DELETE FROM _migrations WHERE name=?", (name,))
    await db.commit()
    await _migrate(db)
    assert await steward_breaker._cursor_of(db, 7) == ""
