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
        self.walk_error = ""
        self.fetch_detail = "network down"

    def commit(
        self, label: str, message: str = "", parent: str | None = None, second: str = ""
    ) -> str:
        sha = sha_of(label)
        parents = (
            [parent] if parent else ([self.history[-1]["sha"]] if self.history else [])
        )
        self.history.append(
            {
                "sha": sha,
                "parents": parents,
                "message": message,
                "second_parent_message": second,
            }
        )
        return sha

    async def fetch_base(self, repo_path: str, base: str):
        return (self.fetch_ok, "" if self.fetch_ok else self.fetch_detail)

    async def first_parent_after(
        self, repo_path: str, base: str, since: str, limit: int
    ):
        if self.walk_error:
            return {"error": self.walk_error}
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


@pytest.fixture(autouse=True)
def _fresh_negative_cache():
    for state in (
        steward_breaker._negative,
        steward_breaker._failures,
        steward_breaker._unchecked,
    ):
        state.clear()
    yield
    for state in (
        steward_breaker._negative,
        steward_breaker._failures,
        steward_breaker._unchecked,
    ):
        state.clear()


@pytest.fixture
def git(monkeypatch) -> FakeGit:
    fake = FakeGit()
    monkeypatch.setattr(plugins, "git_ops", fake)
    return fake


def ci_run(
    conclusion: str, path: str = CI + "@refs/heads/develop", branch: str = "develop"
) -> dict[str, Any]:
    return {
        "head_branch": branch,
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

    m_a, m_b, m_c, m_d, m_e, m_f, m_g = (sha_of(f"merge-{n}") for n in "abcdefg")
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
    task_g = await delivered(db, project, m_g, steward_approval())
    task_f = await delivered(db, project, m_f, [("steward", "approved", None, 2)])

    git.commit("base-0", "chore: start")
    assert await steward_breaker.collect_reverts(db, project) == 0
    head = git.history[-1]["sha"]
    assert await cursor(db, pid) == head, "первый тик ставит курсор и ничего не судит"

    git.commit("revert-a", revert_message(m_a))
    git.commit("revert-b", revert_message(m_b))
    # Без строки «This reverts commit» в теле: тема и упоминание sha - не откат.
    git.commit("revert-c", f'Revert "feat: c"\n\nОткат вручную, см. {m_c}.\n')
    git.commit("revert-d", revert_message(m_d))
    git.commit("revert-e", revert_message(m_e))
    git.commit("revert-f", revert_message(m_f))
    # Откат через PR, влитый merge-коммитом: строка лежит в теле второго родителя.
    git.commit("merge-pr", "Merge pull request #5", second=revert_message(m_g, "pr"))
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

    assert await steward_breaker.collect_reverts(db, project) == 2
    expected = [(task_a, "reverted", m_a), (task_g, "reverted", m_g)]
    assert await cases(db) == expected
    assert await effective_mode(db) == "shadow"
    assert await notices(db, task_a) == (1, 1)
    assert await notices(db, task_g) == (1, 1)
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
    assert await cases(db) == expected
    assert await notices(db, task_a) == (1, 1)

    # Возврат релиза: main->develop приходит merge-коммитом, второй родитель
    # несёт те же тела (сквош с COMMIT_MESSAGES). Ключ случая - доставка, поэтому
    # ни строки, ни уведомления не прибавилось, в том числе после снятия.
    git.commit(
        "release-return-1", "Merge main into develop", second=revert_message(m_a)
    )
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await clear_false_approval(db, task_a, "denis", "разобрано") == 1
    git.commit(
        "release-return-2", "Merge main into develop", second=revert_message(m_a)
    )
    assert await steward_breaker.collect_reverts(db, project) == 0
    assert await cases(db) == expected
    assert await notices(db, task_a) == (1, 1)
    assert [f.task_id for f in await sticky_false_approvals(db)] == [task_g]
    await db.execute(
        "UPDATE steward_false_approvals SET cleared_at=NULL, cleared_by=NULL "
        "WHERE task_id=?",
        (task_a,),
    )
    await db.commit()

    banner = await steward_breaker.banner_rows(db)
    assert [(b["task_id"], b["source"]) for b in banner] == [
        (task_a, "reverted"),
        (task_g, "reverted"),
    ]
    inbox = await dashboard.get_inbox_data(db)
    assert inbox["steward_breaker_banner"] == banner, "видно и без steward_watch"
    quiet = {**inbox, "steward_breaker_banner": []}
    assert dashboard.inbox_attention_total(quiet) == inbox["inbox_attention_total"] - 2


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
    git.commit("short-unique", f"This reverts commit {m_solo[:12]}")
    assert await steward_breaker.collect_reverts(db, project) == 1
    assert await cases(db) == [(task_solo, "reverted", m_solo)]


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
        steward_breaker._negative.clear()
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

    # Переходный unknown не кэшируется: прогон ещё идёт, потом стал красным.
    g5 = git.commit("g5", "chore: parent")
    m5 = git.commit("m5", "feat: fifth delivery")
    task_m5 = await delivered(db, project, m5, steward_approval())
    git.runs[g5] = [ci_run("success")]
    running = ci_run("failure") | {"status": "in_progress", "conclusion": ""}
    git.runs[m5] = [running]
    await tick(branch_run(m5, "failure"))
    assert await cases(db) == []
    git.runs[m5] = [ci_run("failure")]
    git.runs[g5] = [running]  # родитель ещё идёт: снова unknown
    git.branch_runs = [branch_run(m5, "failure")]
    project_row = await repo.get_project(db, int(project["id"]))
    await red_base.check_project(db, project_row)
    assert await cases(db) == []
    git.runs[g5] = [ci_run("success")]
    await red_base.check_project(db, project_row)
    assert [c[:2] for c in await cases(db)] == [(task_m5, "base_red_after_merge")]
    await db.execute("DELETE FROM steward_false_approvals")
    await db.commit()
    # Кэш ограничен по размеру.
    for i in range(steward_breaker._NEGATIVE_MAX + 5):
        steward_breaker._remember_negative((1, str(i)))
    assert len(steward_breaker._negative) <= steward_breaker._NEGATIVE_MAX

    # Отменённый прогон (ручная отмена, concurrency) красным не считается, ни у
    # доставки, ни у родителя; прогон чужой ветки тоже.
    g4 = git.commit("g4", "chore: parent")
    m4 = git.commit("m4", "feat: fourth delivery")
    await delivered(db, project, m4, steward_approval())
    git.runs[g4] = [ci_run("success")]
    for conclusion in ("cancelled", "action_required", "startup_failure"):
        git.runs[m4] = [ci_run(conclusion)]
        await tick(branch_run(m4, "failure"))
    git.runs[m4] = [ci_run("failure", branch="feature/x")]
    await tick(branch_run(m4, "failure"))
    git.runs[m4] = [ci_run("failure")]
    git.runs[g4] = [ci_run("cancelled")]
    await tick(branch_run(m4, "failure"))
    assert await cases(db) == []

    # Отрицательный кэш: пока не истёк, ответ git и форжа заново не спрашивается.
    git.runs[g4] = [ci_run("failure")]  # устойчивый ответ: родитель красный
    await red_base.check_project(db, await repo.get_project(db, int(project["id"])))
    git.runs[g4] = [ci_run("success")]
    await red_base.check_project(db, await repo.get_project(db, int(project["id"])))
    assert await cases(db) == [], "кэш держит прошлый ответ"
    steward_breaker._negative.clear()
    await red_base.check_project(db, await repo.get_project(db, int(project["id"])))
    assert [c[1:] for c in await cases(db)] == [("base_red_after_merge", m4)]
    await db.execute("DELETE FROM steward_false_approvals")
    await db.commit()

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
    """Два отката (двух доставок одной задачи); снятие первого не гасит второй; счётчик за 14 дней.

    Ключ случая - откатанная доставка (P2-A): второй откат ТОЙ ЖЕ доставки был
    бы тем же случаем, поэтому два случая дают два разных слияния.
    """
    project = await project_with_workspace(db, "brk-clear")
    m1, m2 = sha_of("merge-t1"), sha_of("merge-t2")
    task_t = await delivered(db, project, m1, steward_approval())
    await repo.record_pipeline_merge(
        db,
        pr_number=next(_PR),
        merge_sha=m2,
        project_id=int(project["id"]),
        task_id=task_t,
        merged_at="2026-10-20 12:00:00",
    )
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)

    git.commit("revert-1", revert_message(m1, "first"))
    await steward_breaker.collect_reverts(db, project)
    assert await clear_false_approval(db, task_t, "denis", "разобрано") == 1
    assert await sticky_false_approvals(db) == []

    git.commit("revert-2", revert_message(m2, "second"))
    await steward_breaker.collect_reverts(db, project)
    active = [(f.task_id, f.source, f.ref) for f in await sticky_false_approvals(db)]
    assert active == [(task_t, "reverted", m2)], "второй случай остаётся активным"
    assert [c[2] for c in await cases(db)] == [m1, m2]
    assert [b["ref"] for b in await steward_breaker.banner_rows(db)] == [m2]

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


async def test_unreadable_history_is_said_not_silent(
    db: aiosqlite.Connection, git: FakeGit, caplog
):
    """git не ответил после удачного fetch: warning с причиной, через N тиков - сигнал."""
    project = await project_with_workspace(db, "brk-silent")
    pid = int(project["id"])
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    git.walk_error = "rev-list rc=124"

    async def unchecked_events() -> int:
        return await count(
            db,
            "SELECT COUNT(*) FROM events WHERE kind=?",
            steward_breaker.EVENT_UNCHECKED,
        )

    for tick in range(1, steward_breaker.UNCHECKED_AFTER):
        assert await steward_breaker.collect_reverts(db, project) == 0
        assert "rev-list rc=124" in caplog.text
        assert await unchecked_events() == 0, f"тик {tick}: рано"
        assert await steward_breaker.banner_rows(db) == []
    await steward_breaker.collect_reverts(db, project)
    assert await unchecked_events() == 1
    banner = await steward_breaker.banner_rows(db)
    assert [b["source"] for b in banner] == ["unchecked"]
    assert "rev-list rc=124" in banner[0]["line"]
    await steward_breaker.collect_reverts(db, project)
    assert await unchecked_events() == 1, "одно событие на серию"

    git.walk_error = ""
    await steward_breaker.collect_reverts(db, project)
    assert await steward_breaker.banner_rows(db) == []
    assert pid not in steward_breaker._failures


_SCHEME = "https" + "://"
LEAK = (
    f"fatal: unable to access '{_SCHEME}x-access-token:SECRET"
    + "@github.com/o/r.git/': "
    "Could not resolve host\n"
    f"second line {_SCHEME}other:SECRET2" + "@host/x"
)


async def test_the_unchecked_signal_leaks_no_remote_url_or_secret(
    db: aiosqlite.Connection, git: FakeGit, caplog, monkeypatch
):
    monkeypatch.setenv("HUB_TEST_API_KEY", "SUPERSECRETVALUE123")
    project = await project_with_workspace(db, "brk-leak")
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    git.fetch_ok = False
    git.fetch_detail = "fatal: SUPERSECRETVALUE123 " + LEAK
    for _ in range(steward_breaker.UNCHECKED_AFTER):
        await steward_breaker.collect_reverts(db, project)
    banner = (await steward_breaker.banner_rows(db))[0]["line"]
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=?",
        (steward_breaker.EVENT_UNCHECKED,),
    )
    assert len(events) == 1
    for text in (banner, events[0]["payload"], caplog.text):
        for forbidden in ("SECRET", "https://", "github.com", "x-access-token"):
            assert forbidden not in text, (forbidden, text)
    assert "fetch" in banner and len(banner) < 400


async def test_an_exception_on_every_tick_is_a_signal_and_names_only_its_type(
    db: aiosqlite.Connection, git: FakeGit, monkeypatch
):
    project = await project_with_workspace(db, "brk-raise")
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    git.commit("next", "chore: next")

    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("token ghp_SECRETTEXT")

    monkeypatch.setattr(steward_breaker, "_trip_revert", boom)
    for tick in range(1, steward_breaker.UNCHECKED_AFTER):
        assert await steward_breaker.collect_reverts(db, project) == 0
        assert await steward_breaker.banner_rows(db) == []
    await steward_breaker.collect_reverts(db, project)
    banner = await steward_breaker.banner_rows(db)
    assert [b["source"] for b in banner] == ["unchecked"]
    assert "RuntimeError" in banner[0]["line"] and "SECRETTEXT" not in banner[0]["line"]
    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE kind=?",
        (steward_breaker.EVENT_UNCHECKED,),
    )
    assert len(events) == 1 and "SECRETTEXT" not in events[0]["payload"]


def test_a_bare_token_in_the_first_stderr_line_is_masked():
    token = "ghp_" + "A1b2C3d4E5f6G7h8"
    pat = "github_pat_" + "11ABCdef_xyz"
    line = f"fatal: auth failed for {token} and {pat}; Bearer abc.def.ghi"
    shown = steward_breaker._safe(line)
    assert token not in shown and pat not in shown and "abc.def.ghi" not in shown
    assert shown.startswith("fatal: auth failed for")


async def test_a_failed_unchecked_record_is_said_on_the_next_tick(
    db: aiosqlite.Connection, git: FakeGit, monkeypatch
):
    project = await project_with_workspace(db, "brk-said")
    git.commit("base-0")
    await steward_breaker.collect_reverts(db, project)
    git.commit("next", "chore: next")

    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("x")

    monkeypatch.setattr(steward_breaker, "_trip_revert", boom)
    real_insert = repo.insert_event
    calls = {"n": 0}

    async def flaky_insert(*a: Any, **k: Any):
        if k.get("kind") == steward_breaker.EVENT_UNCHECKED and calls["n"] == 0:
            calls["n"] += 1
            raise aiosqlite.OperationalError("database is locked")
        return await real_insert(*a, **k)

    monkeypatch.setattr(steward_breaker.repo, "insert_event", flaky_insert)
    for _ in range(steward_breaker.UNCHECKED_AFTER + 1):
        assert await steward_breaker.collect_reverts(db, project) == 0
    events = await fetchall(
        db, "SELECT id FROM events WHERE kind=?", (steward_breaker.EVENT_UNCHECKED,)
    )
    assert len(events) == 1


# #2139: тесты пометки «не ошибка пары» вынесены в tests/steward_pair_miss_cases.py
# (бюджет размера файла тестов); имена те же, что в test_ref критериев.
from tests.steward_pair_miss_cases import (  # noqa: E402,F401
    hdr,
    test_counter_does_not_trust_the_display_name,
    test_mark_on_a_standing_verdict_is_not_a_silent_repeat,
    test_cli_sends_the_not_pair_miss_mark,
    test_human_return_marked_not_pair_miss_keeps_act,
    test_human_return_without_valid_mark_is_pair_miss,
    test_migration_keeps_other_sources_and_existing_rows,
    test_only_human_with_note_can_mark_not_pair_miss,
)
