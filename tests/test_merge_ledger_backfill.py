"""Досыпка реестра мержей по собственным записям хаба (#1367).

До #1343 реестр ``pipeline_merges`` молча терял мержи гейта: ключ был номер
PR, а после переезда репозитория нумерация началась заново. Сторож базовой
ветки (#534) записал эти доставки в ``base_branch_drift`` как «мимо гейта».

Досыпка возвращает строку ТОЛЬКО по собственной записи хаба о мерже:

* запись hub в ленте задачи «PR #N влит» — строка с task_id;
* активность release о возврате main→develop с тем же sha — task_id NULL.

Номер PR в заголовке коммита доказательством не считается (#534): это текст,
который пишет тот, кто пушит. Настоящие ручные мержи обязаны остаться дрейфом.

Провайдер подменяется двойником: сеть здесь не нужна, а отказ провайдера —
отдельный случай, который прогон обязан пережить.
"""

from __future__ import annotations

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import log_activity
from hub.services.merge_ledger_backfill import backfill_merge_ledger

GATE_SHA = "396c2f7" + "a" * 33
MANUAL_SHA = "a3f5e73" + "b" * 33
RETURN_SHA = "7583dbc" + "c" * 33
BARE_RETURN_SHA = "0d0d0d0" + "d" * 33
REFUSED_SHA = "5e5e5e5" + "e" * 33


class _Provider:
    """Двойник git_ops: отвечает только на поиск PR по мерж-коммиту."""

    def __init__(
        self, prs: dict[str, dict] | None = None, fail: set[str] = frozenset()
    ):
        self.prs = prs or {}
        self.fail = set(fail)
        self.asked: list[str] = []

    async def pr_for_merge_commit(
        self,
        sha: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> dict | None:
        self.asked.append(sha)
        if sha in self.fail:
            raise RuntimeError("HTTP 403: API rate limit exceeded")
        return self.prs.get(sha)


@pytest.fixture
def provider(monkeypatch):
    def _install(**kwargs) -> _Provider:
        double = _Provider(**kwargs)
        from hub.integrations.registry import plugins

        monkeypatch.setattr(plugins, "git_ops", double)
        return double

    return _install


async def _default_project(db: aiosqlite.Connection) -> int:
    project = await repo.get_project_by_slug(db, "default")
    if project:
        return int(dict(project)["id"])
    return int(await repo.create_project(db, slug="default", name="Default"))


async def _task(db: aiosqlite.Connection, *, branch: str, pr_number: int) -> int:
    task_id = await repo.create_task(
        db,
        title="work",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(db, task_id, branch=branch, pr_number=pr_number)
    await db.commit()
    return task_id


async def _drift(db: aiosqlite.Connection, project_id: int, sha: str, subject: str):
    await repo.record_drift_commit(
        db,
        project_id=project_id,
        sha=sha,
        branch="develop",
        subject=subject,
        author="someone",
    )


async def _hub_says_merged(db: aiosqlite.Connection, task_id: int, pr: int) -> None:
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Доставлено хабом без отчёта агента: PR #{pr} влит по одобренному "
        "ревью. Условия доставки были выполнены целиком, ждать было нечего.",
    )
    await db.commit()


async def _merges(db: aiosqlite.Connection) -> list[dict]:
    rows = await repo.fetchall(
        db,
        "SELECT project_id, pr_number, task_id, merge_sha FROM pipeline_merges "
        "ORDER BY id",
    )
    return [dict(r) for r in rows]


async def _gate_delivery(db: aiosqlite.Connection, project_id: int) -> int:
    """#1281 PR 406: гейт влил, реестр потерял, сторож записал дрейф."""
    task_id = await _task(db, branch="task-1281/gate-work", pr_number=406)
    await _hub_says_merged(db, task_id, 406)
    await _drift(db, project_id, GATE_SHA, "feat(task): gate-work (#1281)")
    return task_id


async def _manual_merge(db: aiosqlite.Connection, project_id: int) -> int:
    """#1242 PR 424: влит владельцем руками. Хаб о мерже не писал.

    Две ловушки рядом, обе про номер:

    * агент сам написал в ленту «PR #424 влит» — это не запись хаба;
    * у задачи из ПРЕЖНЕГО репозитория есть настоящая запись хаба
      «PR #424 влит», но это другой PR с тем же номером (#1343).
    """
    task_id = await _task(db, branch="task-1242/by-hand", pr_number=424)
    await repo.add_task_update(
        db,
        task_id,
        "pda_claude",
        "status",
        "PR #424 влит по одобренному ревью",
        author_kind="principal",
        principal_id=None,
    )
    old_repo_task = await _task(db, branch="task-612/old-repo", pr_number=424)
    await _hub_says_merged(db, old_repo_task, 424)
    await _drift(db, project_id, MANUAL_SHA, "feat(task): by-hand (#1242)")
    return task_id


def _prs() -> dict[str, dict]:
    return {
        GATE_SHA: {"number": 406, "head": "task-1281/gate-work", "merge_sha": GATE_SHA},
        MANUAL_SHA: {
            "number": 424,
            "head": "task-1242/by-hand",
            "merge_sha": MANUAL_SHA,
        },
    }


async def test_a_gate_delivery_lost_by_the_ledger_is_restored(db, provider):
    provider(prs=_prs())
    project_id = await _default_project(db)
    task_id = await _gate_delivery(db, project_id)

    report = await backfill_merge_ledger(db, project="default", apply=True)

    assert await _merges(db) == [
        {
            "project_id": project_id,
            "pr_number": 406,
            "task_id": task_id,
            "merge_sha": GATE_SHA,
        }
    ]
    assert report["written"] == 1
    assert [line["sha"] for line in report["restore"]] == [GATE_SHA]
    assert report["restore"][0]["evidence"] == "task_feed"

    again = await backfill_merge_ledger(db, project="default", apply=True)
    assert again["written"] == 0, "повторный --apply ничего не дописывает"
    assert again["restore"] == []
    assert len(await _merges(db)) == 1
    drift = await repo.list_drift_commits(db, project_id, include_ledgered=True)
    assert [dict(r)["sha"] for r in drift] == [GATE_SHA], "история дрейфа не удаляется"


async def test_a_manual_merge_stays_drift(db, provider):
    provider(prs=_prs())
    project_id = await _default_project(db)
    await _manual_merge(db, project_id)

    for apply in (False, True):
        report = await backfill_merge_ledger(db, project="default", apply=apply)
        assert report["restore"] == [], "номер PR не доказательство (#534)"
        assert [line["sha"] for line in report["drift"]] == [MANUAL_SHA]
        assert "нет записи хаба о мерже" in report["drift"][0]["reason"]

    assert await _merges(db) == []


async def test_a_release_return_is_restored_by_its_activity_record(db, provider):
    provider(prs={})
    project_id = await _default_project(db)
    await _drift(db, project_id, RETURN_SHA, "Merge branch 'main' into develop")
    await _drift(db, project_id, BARE_RETURN_SHA, "Merge branch 'main' into develop")
    await log_activity(
        db,
        "release",
        "default: main возвращён в develop после релиза",
        f"merge {RETURN_SHA[:12]}; расхождение закрыто в тот же момент, "
        "пока слияние тривиально",
    )

    report = await backfill_merge_ledger(db, project="default", apply=True)

    assert await _merges(db) == [
        {
            "project_id": project_id,
            "pr_number": 0,
            "task_id": None,
            "merge_sha": RETURN_SHA,
        }
    ]
    assert [line["evidence"] for line in report["restore"]] == ["release_return"]
    assert [line["sha"] for line in report["drift"]] == [BARE_RETURN_SHA], (
        "возврат без записи активности остаётся дрейфом"
    )


async def test_the_default_run_writes_nothing(db, provider):
    provider(prs=_prs())
    project_id = await _default_project(db)
    await _gate_delivery(db, project_id)
    await _manual_merge(db, project_id)

    report = await backfill_merge_ledger(db, project="default")

    assert report["apply"] is False
    assert report["written"] == 0
    assert await _merges(db) == []
    assert [line["sha"] for line in report["restore"]] == [GATE_SHA]
    assert [line["sha"] for line in report["drift"]] == [MANUAL_SHA]


async def test_a_provider_refusal_leaves_the_commit_in_drift_with_its_reason(
    db, provider
):
    provider(prs=_prs(), fail={REFUSED_SHA})
    project_id = await _default_project(db)
    await _gate_delivery(db, project_id)
    await _drift(db, project_id, REFUSED_SHA, "feat(task): whatever (#1300)")

    report = await backfill_merge_ledger(db, project="default", apply=True)

    assert [line["sha"] for line in report["restore"]] == [GATE_SHA]
    assert [line["sha"] for line in report["drift"]] == [REFUSED_SHA]
    assert "403" in report["drift"][0]["reason"]
    assert "провайдер" in report["drift"][0]["reason"]


async def test_a_pr_whose_merge_commit_is_another_commit_is_not_evidence(db, provider):
    """Провайдер обязан назвать ИМЕННО этот коммит мержем этого PR."""
    provider(
        prs={
            GATE_SHA: {
                "number": 406,
                "head": "task-1281/gate-work",
                "merge_sha": "f" * 40,
            }
        }
    )
    project_id = await _default_project(db)
    await _gate_delivery(db, project_id)

    report = await backfill_merge_ledger(db, project="default", apply=True)

    assert report["restore"] == []
    assert await _merges(db) == []


async def test_an_unknown_project_is_refused(db, provider):
    provider()
    with pytest.raises(LookupError):
        await backfill_merge_ledger(db, project="nope")


# --- REST ---------------------------------------------------------------


async def test_rest_backfill_is_admin_only_and_dry_by_default(
    client, db, provider, monkeypatch
):
    provider(prs=_prs())
    project_id = await _default_project(db)
    await _gate_delivery(db, project_id)
    await db.commit()
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "admin-token": TokenIdentity("owner", "admin"),
            "agent-token": TokenIdentity("bot", "agent"),
        },
    )

    refused = await client.post(
        "/api/admin/merge-ledger/backfill",
        json={"project": "default"},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert refused.status_code == 403

    dry = await client.post(
        "/api/admin/merge-ledger/backfill",
        json={"project": "default"},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["apply"] is False
    assert [line["sha"] for line in dry.json()["restore"]] == [GATE_SHA]
    assert await _merges(db) == []

    applied = await client.post(
        "/api/admin/merge-ledger/backfill",
        json={"project": "default", "apply": True},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["written"] == 1

    missing = await client.post(
        "/api/admin/merge-ledger/backfill",
        json={"project": "nope"},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert missing.status_code == 404


# --- CLI ----------------------------------------------------------------


def test_cli_backfill_is_dry_unless_apply_is_named(capsys):
    from unittest.mock import MagicMock, patch

    from hub import cli

    payload = {
        "project": "default",
        "apply": False,
        "written": 0,
        "restore": [
            {
                "sha": GATE_SHA,
                "subject": "feat(task): gate-work (#1281)",
                "reason": "запись хаба в ленте #1281: PR #406 влит",
                "evidence": "task_feed",
                "task_id": 1281,
                "pr_number": 406,
            }
        ],
        "drift": [
            {
                "sha": MANUAL_SHA,
                "subject": "feat(task): by-hand (#1242)",
                "reason": "нет записи хаба о мерже PR #424 (ветка task-1242/by-hand)",
                "evidence": "",
                "task_id": None,
                "pr_number": 424,
            }
        ],
    }
    api = MagicMock(return_value=payload)
    parser = cli.build_parser()
    args = parser.parse_args(["merge-ledger-backfill"])
    with patch.object(cli, "_api", api):
        assert args.func(args) == 0
    api.assert_called_once_with(
        "POST",
        "/api/admin/merge-ledger/backfill",
        {"project": "default", "apply": False},
    )
    out = capsys.readouterr().out
    assert "сухой прогон" in out
    assert f"допишется: {GATE_SHA[:12]}" in out
    assert f"остаётся дрейфом: {MANUAL_SHA[:12]}" in out

    api.reset_mock()
    args = parser.parse_args(["merge-ledger-backfill", "--apply"])
    with patch.object(cli, "_api", api):
        args.func(args)
    assert api.call_args.args[2] == {"project": "default", "apply": True}


# --- провайдер: только чтение, и только мерж-коммит этого PR -----------------


async def test_github_answers_only_with_the_pr_whose_merge_commit_it_is(monkeypatch):
    import json

    from hub.integrations.forge import github

    calls: list[tuple] = []
    answer = [
        # содержит коммит, но влит другим мержем — не ответ
        {"number": 1, "merge_commit_sha": "f" * 40, "merged_at": "x", "head": {}},
        # тот же sha, но не влит — не ответ
        {"number": 2, "merge_commit_sha": GATE_SHA, "merged_at": None, "head": {}},
        {
            "number": 406,
            "merge_commit_sha": GATE_SHA,
            "merged_at": "2026-09-20T10:00:00Z",
            "head": {"ref": "task-1281/gate-work"},
        },
    ]

    async def fake_gh(*args, repo=None, **kw):
        calls.append(args)
        return 0, json.dumps(answer), ""

    monkeypatch.setattr(github, "_gh", fake_gh)
    forge = github.GitHubForge()

    got = await forge.pr_for_merge_commit(GATE_SHA, gh_repo="o/r")

    assert got == {"number": 406, "head": "task-1281/gate-work", "merge_sha": GATE_SHA}
    assert calls == [("api", f"repos/o/r/commits/{GATE_SHA}/pulls")], "только GET"

    async def refused(*args, repo=None, **kw):
        return 1, "", "HTTP 403: rate limit"

    monkeypatch.setattr(github, "_gh", refused)
    with pytest.raises(RuntimeError, match="403"):
        await forge.pr_for_merge_commit(GATE_SHA, gh_repo="o/r")
