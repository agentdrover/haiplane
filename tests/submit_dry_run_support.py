"""Мир для тестов сдачи без записи (#1975).

Pair-задача под проектом с политикой, git-адаптер со своей головой на ветку и
фальшивый GitHub. Файл не импортирует ничего из нового кода хаба: он обязан
загружаться и до задачи, чтобы тесты падали на поведении, а не на ImportError.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import AcceptanceCriterion, TaskRefine
from tests.ci_trust_support import REPO as GH_REPO
from tests.ci_trust_support import GREEN_CHECKS, attach_fake_github

AREAS = ["hub/example.py", "tests/test_example.py"]
REF_1 = "tests/test_example.py::test_one"
REF_2 = "tests/test_example.py::test_two"
UID = "0123456789abcdef"


def tip_of(branch: str) -> str:
    """Голова ветки: у каждой задачи своя, чтобы прогоны CI не путались."""
    return hashlib.sha1(branch.encode()).hexdigest()  # nosec B324 - не секрет


class Git(NoopGitOps):
    def __init__(self) -> None:
        self.fetch_ok = True
        self.paths = list(AREAS)
        self.tips: dict[str, str] = {}

    async def fetch_base(self, repo: str, base: str):
        return (True, "") if self.fetch_ok else (False, "network down")

    async def head_sha(self, repo: str, base: str) -> str:
        return self.tips.get(base) or tip_of(base)

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return list(self.paths)

    async def branch_touched_paths(
        self, branch, base_branch=None, repo=None, head_sha=None
    ):
        return list(self.paths), head_sha or ""


def world() -> SimpleNamespace:
    git = Git()
    plugins.git_ops = git
    return SimpleNamespace(git=git, fake=attach_fake_github(git))


async def _node(db, title: str, task_type: str, parent_id: int | None) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


def _ac(ac_id: str, kind: str, ref: str | None) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=ac_id, given="g", when="w", then="t", verifiable_by=kind, test_ref=ref
    )


async def ready_task(
    db: aiosqlite.Connection,
    slug: str,
    policy: dict[str, Any],
    work_type: str = "feature",
) -> tuple[int, str]:
    """Pair-задача в running: два AC с тестом и один ручной; ``(id, голова)``."""
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws", repo_name=GH_REPO
    )
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    epic = await _node(db, "epic", "epic", None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, "feature", "feature", epic)
    task_id = await _node(db, "task", "task", feature)
    await repo.update_task(db, task_id, work_type=work_type)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=AREAS))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-1", "test", REF_1))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-2", "test", REF_2))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-3", "manual", None))
    await db.commit()
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    branch = dict(await repo.get_task(db, task_id))["branch"]
    return task_id, tip_of(branch)


def body(**extra: Any) -> dict[str, Any]:
    """Тело сдачи, которое проходит submission_contract=require."""
    return {
        "agent": "dev",
        "model": "claude-sonnet-5-5",
        "summary": "сделано",
        "mutations": [
            {"ac": "AC-1", "mutation": "m1", "failed_test": REF_1},
            {"ac": "AC-2", "mutation": "m2", "failed_test": REF_2},
        ],
        **extra,
    }


async def post_ci(
    client: AsyncClient,
    w: SimpleNamespace,
    task_id: int,
    tip: str,
    *,
    conclusion: str = "success",
    baseline: dict[str, Any] | None = None,
):
    """Отчёт CI о голове — как его шлёт репортёр, с прогоном на фальшивом GitHub."""
    ac = {"AC-1": "pass", "AC-2": "pass"}
    sent = w.fake.attempt(
        task_id,
        tip,
        ac_results=ac,
        conclusion=conclusion,
        baseline=baseline,
        stamp=False,
    )
    report = {
        "head_sha": tip,
        "ac_results": ac,
        "checks": dict(GREEN_CHECKS),
        "validation_status": "pass",
        **({"baseline": baseline} if baseline else {}),
        **sent["provenance"],
    }
    tokens = config.parse_tokens("ci:ci-token:admin")
    with (
        patch.object(config, "HUB_TOKENS", tokens),
        patch.object(config, "HUB_AUTH_DISABLED", False),
    ):
        resp = await client.post(
            f"/api/tasks/{task_id}/ci-run-report",
            json=report,
            headers={"Authorization": "Bearer ci-token"},
        )
    assert resp.status_code == 200, resp.text
    return resp


#: Время последнего использования ключа и принципала — учёт самой
#: аутентификации (ключ человека в мире заказов), меняется по времени и не часть
#: сдачи. Остальные колонки этих таблиц сравниваются: отзыв ключа будет виден.
USE_TIMES = {"api_keys": ("last_used_at",), "principals": ("last_seen_at",)}


async def snapshot(
    db: aiosqlite.Connection, skip: tuple[str, ...] = ()
) -> dict[str, list[str]]:
    """Содержимое таблиц: правка существующей строки видна, а не только рост."""
    await db.commit()
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    dump: dict[str, list[str]] = {}
    for (name,) in await cur.fetchall():
        if name in skip:
            continue
        cursor = await db.execute(f'SELECT * FROM "{name}"')
        cols = [d[0] for d in cursor.description]
        masked = USE_TIMES.get(name, ())
        dump[name] = sorted(
            repr(tuple(None if c in masked else v for c, v in zip(cols, row)))
            for row in await cursor.fetchall()
        )
    return dump


def changed(before: dict, after: dict) -> list[str]:
    return [name for name in after if after[name] != before.get(name)]


@contextmanager
def tripwires():
    """Всё, что dry-run обязан не вызвать: запись о вызове, а не исключение.

    Исключение гасят ``except Exception`` рядом с вызовами, поэтому падение
    заглушки осталось бы незамеченным; запись видна тесту всегда.
    """
    calls: list[str] = []

    async def pr_for_branch(*a, **k):
        calls.append("pr_for_branch")

    async def ensure_delivery_pr(*a, **k):
        calls.append("ensure_delivery_pr")
        return None, ""

    async def dispatch(*a, **k):
        calls.append("maybe_dispatch_review")

    with (
        patch.object(plugins.git_ops, "pr_for_branch", pr_for_branch, create=True),
        patch("hub.services.orchestration.ensure_delivery_pr", ensure_delivery_pr),
        patch("hub.services.review_dispatch.maybe_dispatch_review", dispatch),
    ):
        yield calls


async def submit(
    client: AsyncClient,
    task_id: int,
    payload: dict[str, Any],
    *,
    dry: bool,
    headers: dict[str, str] | None = None,
):
    url = f"/api/tasks/{task_id}/submit-review" + ("?dry_run=1" if dry else "")
    return await client.post(url, json=payload, headers=headers or {})


def same(a, b) -> bool:
    """Один код и одно тело ответа."""
    return a.status_code == b.status_code and a.json() == b.json()


async def pend(db: aiosqlite.Connection, task_id: int) -> None:
    """Сохранённая проекция CI — pending, а GitHub уже подтверждает прогон."""
    await db.execute(
        "UPDATE ci_run_reports SET trust='unverified', trust_reason='pending', "
        "attempt_key='' WHERE task_id=?",
        (task_id,),
    )
    await db.execute(
        "UPDATE ci_report_attempts SET trust='unverified', trust_reason='pending', "
        "trust_final=0 WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
