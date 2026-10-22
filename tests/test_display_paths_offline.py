"""Пути показа не ходят в сеть (#1913).

Карточка перерисовывается каждые 15 с, бриф и hub_task_status читают постоянно:
каждый показ с git fetch / ls-remote / gh — нагрузка на GitHub и зависание
страницы. Показ строится из СОХРАНЁННЫХ наблюдений (вершина ветки, CI, PR,
доставка) и там, где наблюдения нет, говорит «не сверено», а не идёт проверять.

Перехват стоит на трёх уровнях, чтобы новый путь в сеть не прошёл мимо:

* адаптер ``plugins.git_ops`` и ``plugins.github`` — любой метод, которого нет в
  списке ЛОКАЛЬНЫХ чтений (``LOCAL_READS``), считается сетевым; новый метод
  протокола без записи в списке красит тест;
* ``proc.run`` — настоящий ``GitOpsIntegration`` на фальшивом процессе: любая
  команда ``git fetch/ls-remote/pull/push/clone`` и любой ``gh`` — нарушение;
* ``socket.connect`` — страховка от прямого http мимо адаптеров.

Гейты сдачи, вердикта и доставки по-прежнему ходят за свежим наблюдением —
это проверяет ``test_gates_still_observe_fresh`` (AC-2).
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.integrations import proc
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.protocols import GitOpsPlugin
from hub.integrations.registry import plugins
from tests.test_decision_brief_whitelist import _task

#: Методы git_ops, читающие только локальный клон. Всё остальное на пути показа
#: считается сетевым. Новый метод протокола попадает в нарушение, пока его не
#: отнесли сюда сознательно (см. ``test_every_adapter_method_is_classified``).
LOCAL_READS = frozenset(
    {
        "branch_ancestry",
        "branch_diff_local",
        "branch_touched_paths",
        "commit_diff",
        "commit_diff_stat",
        "commit_exists",
        "commit_in_base_history",
        "commit_with_same_tree",
        "current_branch",
        "dirty_paths",
        "file_at_ref",
        "files_at_ref",
        "files_naming_at_ref",
        "first_parent_log",
        "head_sha",
        "is_ancestor",
        "merge_preserves_ancestry",
        "read_file_at_ref",
        "resolve_ref",
        "unpushed_commits",
        "worktree_is_registered",
        "worktree_path",
    }
)

#: Методы протокола, которые ходят в сеть или меняют remote. Отдельный список —
#: для проверки полноты классификации.
NETWORK_OR_WRITE = frozenset(
    set(
        name
        for name in dir(GitOpsPlugin)
        if not name.startswith("_") and callable(getattr(GitOpsPlugin, name))
    )
    - LOCAL_READS
)

_NETWORK_GIT = frozenset({"fetch", "ls-remote", "pull", "push", "clone", "remote"})


class NetSpy:
    """Обёртка над адаптером: пишет вызовы всего, что не локальное чтение."""

    def __init__(self, inner: Any, label: str, calls: list[str]) -> None:
        self._inner = inner
        self._label = label
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name.startswith("_") or not callable(attr):
            return attr
        if self._label == "git_ops" and name in LOCAL_READS:
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(f"{self._label}.{name}")
            return attr(*args, **kwargs)

        return wrapper


def _argv_is_network(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    if argv[0] in {"gh", "curl", "ssh"}:
        return True
    if argv[0] != "git":
        return False
    rest = list(argv[1:])
    if rest[:1] == ["-C"]:
        rest = rest[2:]
    return bool(rest) and rest[0] in _NETWORK_GIT


def arm(monkeypatch) -> list[str]:
    """Включить перехват (после подготовки мира) и вернуть список нарушений."""
    calls: list[str] = []

    def guard(name: str):
        def _blocked(self, *a, **k):
            calls.append(f"socket.{name}")
            raise OSError("сеть на пути показа запрещена")

        return _blocked

    monkeypatch.setattr(socket.socket, "connect", guard("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", guard("connect_ex"))
    plugins.git_ops = NetSpy(plugins.git_ops, "git_ops", calls)  # type: ignore[assignment]
    plugins.github = NetSpy(plugins.github, "github", calls)  # type: ignore[assignment]
    return calls


def _is_network_cmd(cmd: tuple[str, ...]) -> bool:
    """Сетевая команда: git fetch/ls-remote/…, gh, либо та же строка внутри ``sh -c``."""
    if _argv_is_network(cmd):
        return True
    return bool(_NETWORK_WORDS.search(" ".join(cmd)))


_NETWORK_WORDS = re.compile(r"\b(fetch|ls-remote|pull|push|clone)\b|(^|\s)gh\s")


def arm_process(monkeypatch, calls: list[str]) -> None:
    """Настоящий GitOpsIntegration на фальшивом процессе: любая сетевая команда — нарушение.

    Локальное чтение ссылок отвечает успехом (вершина — 40 символов ``a``), чтобы
    пути не обрывались до сетевого шага, а шли так далеко, как идут в проде.
    """

    def answer(cmd: tuple[str, ...]) -> tuple[int, str, str]:
        if _is_network_cmd(cmd):
            calls.append("proc " + " ".join(cmd[:6]))
            return 1, "", "offline"
        if "rev-parse" in cmd:
            return 0, "a" * 40, ""
        return 1, "", "offline"

    async def fake_run(*cmd: str, **_kw: Any) -> tuple[int, str, str]:
        return answer(cmd)

    async def fake_bytes(*cmd: str, **_kw: Any) -> tuple[int, bytes, str]:
        rc, out, err = answer(cmd)
        return rc, out.encode(), err

    monkeypatch.setattr(proc, "run", fake_run)
    monkeypatch.setattr(proc, "run_bytes", fake_bytes)
    monkeypatch.setattr(proc, "run_capped", fake_bytes, raising=False)
    plugins.git_ops = NetSpy(GitOpsIntegration(), "git_ops", calls)  # type: ignore[assignment]


async def _world(client, db: aiosqlite.Connection, monkeypatch) -> dict[str, int]:
    """Задачи для всех путей: сдача на ревью с PR, сосед для stacking, завершённая."""
    review_id, _ = await _task(client, db, monkeypatch, "offline-review", 1)
    review = dict(await repo.get_task(db, review_id) or {})
    await db.execute("UPDATE tasks SET pr_number=77 WHERE id=?", (review_id,))
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    # Путь реального проекта: вердикт делегирован (автопилот), скана целостности CI
    # нет — раньше оба условия вели карточку в сеть мимо остальных блоков.
    project_row = dict(await repo.resolve_project_for_task(db, review_id) or {})
    await repo.update_project(
        db, project_row["id"], gate_policy=json.dumps({"verdict": "auto"})
    )
    await db.execute("DELETE FROM ci_integrity_scans WHERE task_id=?", (review_id,))
    # Отчёт CI ещё не подтверждён GitHub: обновление стояния ходило бы за прогонами.
    await repo.upsert_ci_report_attempt(
        db,
        task_id=review_id,
        head_sha=review["submission_sha"],
        run_id="101",
        run_attempt="1",
        ac_results="{}",
        checks="[]",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
        mutations=None,
        baseline=None,
        provenance="{}",
        submitted="{}",
    )
    neighbour = (await client.post("/api/tasks", json={"title": "сосед"})).json()["id"]
    await db.execute(
        "UPDATE tasks SET status='running', branch=? WHERE id=?",
        ("task-9999/neighbour", neighbour),
    )
    # Завершённая задача: мерж записан, выкат — коммит, которого в копии нет.
    done = (await client.post("/api/tasks", json={"title": "готовая"})).json()["id"]
    await db.execute(
        "UPDATE tasks SET status='completed', branch=?, submission_sha=?, "
        "pr_number=78 WHERE id=?",
        ("task-9998/done", "c" * 40, done),
    )
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 78, done, "d" * 40),
    )
    await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, ref, status, source) "
        "VALUES (NULL, ?, 'refs/heads/main', 'success', 'ci')",
        ("e" * 40,),
    )
    # Задача с завершённым job: hub_task_status раньше будил refresh.
    with_job = (await client.post("/api/tasks", json={"title": "с прогоном"})).json()[
        "id"
    ]
    await db.execute(
        "UPDATE tasks SET status='running', job_id='job-done' WHERE id=?",
        (with_job,),
    )
    # Все задачи мира — в проекте сдачи (там рабочая копия): иначе карточка
    # завершённой задачи отвечала бы «у проекта нет копии» и до git не дошла.
    project = dict(await repo.resolve_project_for_task(db, review_id) or {})
    for other in (neighbour, done, with_job):
        await db.execute(
            "UPDATE tasks SET project_id=? WHERE id=?", (project["id"], other)
        )
    # #1927: задача сверяется с выкатом СВОЕГО проекта, поэтому выкат мира
    # принадлежит проекту сдачи (раньше его брали как «новейший любой»).
    await db.execute(
        "UPDATE releases SET project_id=? WHERE project_id IS NULL", (project["id"],)
    )
    await db.commit()
    assert review["branch"]
    return {"review": review_id, "done": done, "job": with_job, "neighbour": neighbour}


class Walk:
    """Обход путей: нарушения подписываются путём, на котором случились."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.found: list[str] = []

    async def step(self, label: str, coro: Any) -> Any:
        before = len(self.calls)
        result = await coro
        self.found.extend(f"{label}: {c}" for c in self.calls[before:])
        return result


async def _display_paths(client, ids: dict[str, int], monkeypatch, walk: Walk) -> None:
    """Каждый путь показа из AC-1, по порядку."""
    from hub import mcp_server, services

    review, done, job = ids["review"], ids["done"], ids["job"]
    for name, task_id in (("review", review), ("done", done), ("job", job)):
        # Полная карточка и HTMX-перерисовка (тот же обработчик, заголовок HX).
        card = await walk.step(f"card {name}", client.get(f"/tasks/{task_id}"))
        assert card.status_code == 200, card.text
        htmx = await walk.step(
            f"htmx {name}",
            client.get(f"/tasks/{task_id}", headers={"HX-Request": "true"}),
        )
        assert htmx.status_code == 200
        api = await walk.step(f"api task {name}", client.get(f"/api/tasks/{task_id}"))
        assert api.status_code == 200
        deps = await walk.step(
            f"api deps {name}", client.get(f"/api/tasks/{task_id}/dependencies")
        )
        assert deps.status_code == 200
    # Бриф: REST для сдачи с PR и для соседа (stacking).
    for name, task_id in (("review", review), ("neighbour", ids["neighbour"])):
        brief = await walk.step(
            f"brief rest {name}", client.get(f"/api/tasks/{task_id}/review-brief")
        )
        assert brief.status_code == 200, brief.text
    diff = await walk.step("diff page", client.get(f"/tasks/{review}/diff"))
    assert diff.status_code == 200, diff.text
    # Входящие.
    inbox = await walk.step("inbox partial", client.get("/partials/inbox"))
    assert inbox.status_code == 200
    await walk.step("inbox data", services.get_inbox_data(plugins_db(client)))

    # MCP: оба инструмента ходят в REST хаба; их собственный POST /refresh — сеть.
    posted: list[str] = []

    async def api_get(path: str, *a: Any, **k: Any) -> Any:
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        return resp.json()

    async def api_post(path: str, *a: Any, **k: Any) -> Any:
        posted.append(path)
        resp = await client.post(path, json=k.get("json") or {})
        return resp.json()

    monkeypatch.setattr(mcp_server, "_api_get", api_get)
    monkeypatch.setattr(mcp_server, "_api_post", api_post)
    await walk.step("mcp brief", mcp_server.hub_get_review_brief(review))
    await walk.step("mcp status job", mcp_server.hub_task_status(job))
    await walk.step("mcp status full", mcp_server.hub_task_status(review, full=True))
    await walk.step("mcp status done", mcp_server.hub_task_status(done))
    walk.found.extend(f"mcp hub_task_status POST {p}" for p in posted)


def plugins_db(client) -> aiosqlite.Connection:
    return client._transport.app.state.db  # type: ignore[attr-defined,no-any-return]


@pytest.mark.parametrize("real_adapter", [False, True])
async def test_no_display_path_touches_the_network(
    client, db: aiosqlite.Connection, monkeypatch, real_adapter: bool
):
    """AC-1: ни одного сетевого вызова на путях показа, ни у адаптеров, ни у процесса."""
    ids = await _world(client, db, monkeypatch)
    monkeypatch.setattr(plugins.dispatch, "get_job", _completed_job, raising=False)
    if not real_adapter:
        # Выкат назван коммитом, которого в копии нет: раньше карточка шла его fetch.
        monkeypatch.setattr(
            plugins.git_ops, "commit_exists", AsyncMock(return_value=False)
        )
    calls = arm(monkeypatch)
    if real_adapter:
        arm_process(monkeypatch, calls)
    walk = Walk(calls)
    await _display_paths(client, ids, monkeypatch, walk)
    assert walk.found == [], "показ ходит в сеть:\n  " + "\n  ".join(
        sorted(set(walk.found))
    )


def _completed_job(job_id: str) -> dict[str, Any]:
    return {"status": "completed", "exit_code": 0, "result_text": "готово"}


async def test_gates_still_observe_fresh(client, db: aiosqlite.Connection, monkeypatch):
    """AC-2: сдача, вердикт и доставка по-прежнему берут свежее наблюдение."""
    from hub.services import lifecycle
    from hub.services.delivery_state import delivery_state
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    monkeypatch.setattr(plugins.git_ops, "commit_exists", AsyncMock(return_value=False))
    calls = arm(monkeypatch)
    review = ids["review"]
    branch = dict(await repo.get_task(db, review) or {})["branch"]

    # Гейт (сдача, вердикт, доставка читают вершину через resolve_branch_tip).
    tip, why = await lifecycle.resolve_branch_tip(db, review, branch)
    assert tip, why
    assert "git_ops.fetch_base" in calls

    # Ветка ушла: показ ещё говорит по памяти, гейт видит новую вершину.
    async def moved(*_a, **_k):
        return "f" * 40

    monkeypatch.setattr(plugins.git_ops._inner, "head_sha", moved)
    shown = await build_review_brief(db, review, observe_only=True)
    assert shown is not None and shown.sha_check == "match"
    fresh_tip, _ = await lifecycle.resolve_branch_tip(db, review, branch)
    assert fresh_tip == "f" * 40
    after = await build_review_brief(db, review, observe_only=True)
    assert after is not None and after.sha_check == "diverged"

    # Явная сборка (стюард, гейты) спрашивает свежо: CI, PR, стыковку.
    calls.clear()
    await build_review_brief(db, review)
    for name in ("fetch_base", "check_pr_mergeable", "ci_runs_for_sha"):
        assert f"git_ops.{name}" in calls, (name, calls)

    # Доставка ходит за недостающим коммитом выката.
    calls.clear()
    await delivery_state(db, ids["done"])
    assert "git_ops.fetch_commit" in calls


async def test_brief_without_observation_says_not_checked(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Нет наблюдения (рестарт хаба): «не сверено», не match и не «чисто»."""
    from hub.services import gate_observations, lifecycle
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    lifecycle.forget_observed_tips()
    gate_observations.forget_all()
    calls = arm(monkeypatch)
    brief = await build_review_brief(db, ids["review"], observe_only=True)
    assert brief is not None and calls == []
    assert brief.sha_check == "unknown"
    assert brief.base_merge.state == "unknown"
    assert "не сверено" in brief.base_merge.reason
    assert "не сверена" in brief.stacking_warning


async def test_observation_is_shown_with_its_age(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Явная сборка запоминает ответы сети; показ повторяет их с возрастом."""
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    review = ids["review"]
    from hub.integrations.protocols import MergeabilityOutcome

    monkeypatch.setattr(
        plugins.git_ops,
        "check_pr_mergeable",
        AsyncMock(return_value=(MergeabilityOutcome.mergeable, "")),
    )
    live = await build_review_brief(db, review)  # платит сетью и запоминает
    assert live is not None and live.base_merge.state == "clean"
    calls = arm(monkeypatch)
    shown = await build_review_brief(db, review, observe_only=True)
    assert shown is not None and calls == []
    assert shown.base_merge.state == "clean"
    assert "сверено менее минуты назад" in shown.base_merge.reason
    assert "по наблюдению хаба" in shown.sha_check_reason


def test_every_adapter_method_is_classified():
    """Новый метод протокола git_ops обязан быть отнесён к локальным чтениям или сетевым."""
    names = {
        n
        for n in dir(GitOpsPlugin)
        if not n.startswith("_") and callable(getattr(GitOpsPlugin, n))
    }
    assert LOCAL_READS <= names, LOCAL_READS - names
    assert NETWORK_OR_WRITE == names - LOCAL_READS


async def test_poller_sweep_feeds_the_display(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Проход поллера платит сетью за наблюдения; после него показ их знает."""
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services import gate_observations, lifecycle
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    review = ids["review"]
    monkeypatch.setattr(
        plugins.git_ops,
        "check_pr_mergeable",
        AsyncMock(return_value=(MergeabilityOutcome.mergeable, "")),
    )
    lifecycle.forget_observed_tips()
    gate_observations.forget_all()
    gate_observations._refreshed_at.clear()
    calls = arm(monkeypatch)
    await gate_observations.sweep(db)
    assert "git_ops.check_pr_mergeable" in calls
    calls.clear()
    brief = await build_review_brief(db, review, observe_only=True)
    assert brief is not None and calls == []
    assert brief.sha_check == "match"
    assert brief.base_merge.state == "clean"
    # Стыковку mock-адаптер не умеет (probe_unsupported) — это незнание, и оно
    # не запоминается как «чисто».
    assert "не определена" in brief.stacking_warning  # спрашивали, ответа не было
    gate_observations.record(
        gate_observations.STACKING,
        review,
        gate_observations.stacking_key(
            dict(await repo.get_task(db, review) or {})["branch"],
            dict(await repo.get_task(db, review) or {})["submission_sha"],
        ),
        {"message": "ADVISORY branch stacking: пример", "outcome": "stacked"},
    )
    stacked = await build_review_brief(db, review, observe_only=True)
    assert stacked is not None
    assert "ADVISORY branch stacking: пример [сверено менее минуты назад]" in (
        stacked.stacking_warning
    )


async def test_diff_base_freshness_is_not_asked_when_offline(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Сверка базы с remote (ls-remote) — только вне показа; показ называет «не сверена»."""
    from hub.services import review_evidence

    ids = await _world(client, db, monkeypatch)
    review = ids["review"]
    branch = dict(await repo.get_task(db, review) or {})["branch"]
    asked: list[str] = []

    async def resolved(*_a, **_k):
        return ("resolved", "a" * 40)

    async def fresh(*_a, **_k):
        asked.append("base_freshness")
        return ("current", "")

    monkeypatch.setattr(plugins.git_ops, "resolve_ref", resolved)
    monkeypatch.setattr(plugins.git_ops, "base_freshness", fresh, raising=False)
    shown = await review_evidence.resolve_diff_base(db, review, branch, offline=True)
    assert asked == []
    assert "не сверена с remote" in shown["reason"]
    live = await review_evidence.resolve_diff_base(db, review, branch)
    assert asked == ["base_freshness"] and live["reason"] == ""
    # Страница диффа (/tasks/{id}/diff) — тот же запрет.
    page = await client.get(f"/tasks/{review}/diff")
    assert page.status_code == 200, page.text
    assert asked == ["base_freshness"], "страница диффа спросила свежесть базы"


async def test_poller_sweep_scans_integrity_and_shows_clear_stacking_with_age(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Скан целостности CI делает проход поллера; «стыковки нет» показывается с возрастом."""
    from hub import repository_ci_integrity as store
    from hub.services import gate_observations
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    review = ids["review"]
    task = dict(await repo.get_task(db, review) or {})
    assert await store.get_ci_integrity_scan(db, review, task["submission_sha"]) is None
    gate_observations._refreshed_at.clear()
    await gate_observations.sweep(db)
    row = await store.get_ci_integrity_scan(db, review, task["submission_sha"])
    assert row and dict(row)["state"] in ("clean", "signs"), row

    gate_observations.record(
        gate_observations.STACKING,
        review,
        gate_observations.stacking_key(task["branch"], task["submission_sha"]),
        {"message": "", "outcome": "clear"},
    )
    shown = await build_review_brief(db, review, observe_only=True)
    assert shown is not None
    assert "Стыковки с чужими ветками нет [сверено" in shown.stacking_warning


async def test_poller_sweep_keeps_to_its_budget_and_backs_off_on_a_dead_fetch(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Общий дедлайн прохода; упавший fetch клона откладывает проход, а не бьётся каждый тик."""
    from hub.services import gate_observations

    await _world(client, db, monkeypatch)
    calls = arm(monkeypatch)
    gate_observations._refreshed_at.clear()

    monkeypatch.setattr(gate_observations, "SWEEP_BUDGET_SECONDS", 0)
    await gate_observations.sweep(db)
    assert calls == [], "бюджета нет — сети нет"

    monkeypatch.setattr(gate_observations, "SWEEP_BUDGET_SECONDS", 25)
    monkeypatch.setattr(
        plugins.git_ops._inner,
        "fetch_base",
        AsyncMock(return_value=(False, "network down")),
    )
    await gate_observations.sweep(db)
    assert "git_ops.fetch_base" in calls
    attempts = len(calls)
    gate_observations._refreshed_at.clear()
    await gate_observations.sweep(db)
    assert len(calls) == attempts, "в окне отката проход не ходит в сеть"


async def test_the_deadline_wraps_only_the_network_not_the_database(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Таймаут ставится на сетевые вызовы; чтения базы (контекст проекта) снаружи него."""
    from hub.services import gate_observations

    from hub import services

    await _world(client, db, monkeypatch)
    # Стыковка по решению (#1913) идёт целиком под дедлайном, с чтением базы
    # внутри, — здесь проверяются остальные шаги, поэтому она заглушена.
    monkeypatch.setattr(
        services, "detect_branch_stacking", AsyncMock(return_value=None)
    )
    gate_observations._refreshed_at.clear()
    inside: list[str] = []
    original = gate_observations._Deadline.run

    async def watch(self, make, ceiling=gate_observations.CALL_CEILING_SECONDS):
        # Внутри обёртки не должно быть чтения проекта из базы.
        import hub.services.orchestration as orch

        real = orch.project_git_context

        async def spy(*a, **k):
            inside.append("project_git_context")
            return await real(*a, **k)

        monkeypatch.setattr(orch, "project_git_context", spy)
        try:
            return await original(self, make, ceiling)
        finally:
            monkeypatch.setattr(orch, "project_git_context", real)

    monkeypatch.setattr(gate_observations._Deadline, "run", watch)
    await gate_observations.sweep(db)
    assert inside == [], f"чтение базы под дедлайном: {inside}"


async def test_a_question_asked_without_an_answer_is_not_shown_as_never_asked(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Спросили, ответа не было: показ говорит «спрашивали», а не «не проверял»."""
    from hub.services import gate_observations
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    gate_observations._refreshed_at.clear()
    await gate_observations.sweep(db)  # mock-адаптер: PR против базы — unknown
    shown = await build_review_brief(db, ids["review"], observe_only=True)
    assert shown is not None
    assert shown.base_merge.state == "unknown"
    assert "спрашивали, ответа не было" in shown.base_merge.reason
    assert "не проверял" not in shown.base_merge.reason


async def test_a_timeout_is_not_recorded_as_an_observation(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Таймаут вопроса о PR — не наблюдение: в «спрашивали, ответа не было» он не идёт."""
    from hub.services import gate_observations
    from hub.services.review_brief import build_review_brief

    ids = await _world(client, db, monkeypatch)
    review = ids["review"]

    asked: list[str] = []

    async def hang(*_a, **_k):
        asked.append("check_pr_mergeable")
        raise TimeoutError

    monkeypatch.setattr(plugins.git_ops, "check_pr_mergeable", hang)
    gate_observations._refreshed_at.clear()
    await gate_observations.sweep(db)
    assert asked, "проход не дошёл до вопроса о PR — тест пуст"
    task = dict(await repo.get_task(db, review) or {})
    assert (
        gate_observations.recall(
            gate_observations.BASE_MERGE,
            review,
            f"{task['pr_number']}@{task['submission_sha']}",
        )
        is None
    )
    shown = await build_review_brief(db, review, observe_only=True)
    assert shown is not None and "спрашивали" not in shown.base_merge.reason


async def test_stacking_in_the_sweep_runs_under_the_deadline(
    client, db: aiosqlite.Connection, monkeypatch
):
    """Стыковка в проходе идёт через дедлайн: зависшая — откат по проекту, не зависание тика."""
    import asyncio

    from hub import services
    from hub.services import gate_observations

    await _world(client, db, monkeypatch)

    entered: list[str] = []

    async def slow(*_a, **_k):
        entered.append("stacking")
        await asyncio.sleep(5)

    monkeypatch.setattr(services, "detect_branch_stacking", slow)
    monkeypatch.setattr(gate_observations, "CALL_CEILING_SECONDS", 0.05)
    monkeypatch.setattr(
        gate_observations._Deadline.run,
        "__defaults__",
        (0.05,),
    )
    gate_observations._refreshed_at.clear()
    await asyncio.wait_for(gate_observations.sweep(db), timeout=4)
    assert entered, "проход не дошёл до стыковки — тест пуст"
    assert gate_observations._down_until, "зависшая стыковка должна дать откат"
