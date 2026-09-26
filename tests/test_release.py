"""Возврат релиза в интеграционную ветку идёт PR-ом, а не мимо правил (#1426).

До задачи хаб возвращал main в develop прямым мержем: ``merge_branches`` →
``POST repos/{repo}/merges``. Мерж без PR правило ``pull_request`` ruleset
21764773 отклоняет, и он проходил только потому, что у роли Write в bypass
стоял режим Always allow. Этим же обходом мог воспользоваться любой писатель,
в том числе облачный исполнитель, и залить код в main или develop мимо PR и
зелёного CI.

Теперь возврат идёт как любая другая доставка. Сразу после релиза хаб
открывает PR main → develop. Следующим тиком поллера он вливает этот PR, если
CI зелёный и PR сливается. Вливает мерж-коммитом, а не squash: иначе коммит
релиза так и не станет предком develop, и расхождение #969 будет копиться
снова. Обход после этого не нужен никому, и владелец снимает роль Write из
bypass.

Красный CI или конфликт на PR возврата релиз не останавливают. Код уже в
проде, и принять неудачу возврата за неудачу релиза значило бы вернуть на
доработку задачу, которая уже раскатана.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)
from hub.integrations.registry import plugins
from hub.services.release import merge_ready_release

RELEASE_PR = 83
RETURN_PR = 91
RETURN_SHA = "a" * 40


def _git(
    *,
    release_pr: int | None = RELEASE_PR,
    return_pr: int | None = None,
    return_ci: CIProbeOutcome = CIProbeOutcome.passed,
    return_mergeable: tuple[MergeabilityOutcome, str] = (
        MergeabilityOutcome.mergeable,
        "clean",
    ),
    return_merge: tuple[bool, str] = (True, RETURN_SHA),
    opened_return: int | None = RETURN_PR,
    return_lookup_failure: str = "",
):
    """Плагин git, где релизный PR зелёный, а PR возврата — как скажут.

    Объявлен КАЖДЫЙ вопрос релизного пути. Незаявленный метод ушёл бы в noop,
    и любой случай читался бы как «не смог посмотреть» — ловушка, на которой
    #968 потерял семь тестов.

    ``merge_branches`` и ``return_release_into_base`` — шпионы на старом пути
    прямого мержа. У настоящего плагина этих методов больше нет. Шпионы здесь,
    чтобы возврат того пути, если кто-то его вернёт, был пойман по вызову.
    """
    g = NoopGitOps()

    async def ci(pr_number, **_kw):
        outcome = return_ci if pr_number == return_pr else CIProbeOutcome.passed
        return CIProbeResult(outcome, f"ci of #{pr_number}")

    async def mergeable(pr_number, **_kw):
        if pr_number == return_pr:
            return return_mergeable
        return (MergeabilityOutcome.mergeable, "clean")

    g.check_pr_ci = AsyncMock(side_effect=ci)
    g.check_pr_mergeable = AsyncMock(side_effect=mergeable)
    g.pr_for_branch = AsyncMock(return_value=release_pr)
    g.open_pr_between = AsyncMock(return_value=(return_pr, return_lookup_failure))
    g.merge_pr = AsyncMock(return_value=True)
    g.merge_return_pr = AsyncMock(return_value=return_merge)
    g.merge_commit_sha = AsyncMock(return_value="c" * 40)
    g.content_differs = AsyncMock(return_value=release_pr is not None)
    g.ensure_remote_branch = AsyncMock(return_value=("present", "b" * 12))
    # Релизный PR открывается старым путём, PR возврата — своим (#1426):
    # по паре база+голова и только из этого же репозитория.
    g.open_release_pr = AsyncMock(return_value=777)
    g.open_return_pr = AsyncMock(
        return_value=(opened_return, "" if opened_return else "форж отказал")
    )
    g.merge_branches = AsyncMock(return_value=("returned", "d" * 40))
    g.return_release_into_base = AsyncMock(return_value=("returned", "d" * 40))
    plugins.git_ops = g
    return g


async def _project(db: aiosqlite.Connection) -> aiosqlite.Row:
    pid = await repo.create_project(db, slug="shipper", name="Shipper")
    await repo.update_project(
        db,
        pid,
        gate_policy=json.dumps({"release": "auto"}),
        workspace_path="/tmp/shipper",
        repo="agentdrover/haiplane",
    )
    await db.commit()
    row = await repo.get_project(db, pid)
    assert row is not None
    return row


async def _return_rows(db: aiosqlite.Connection) -> list[dict]:
    return [
        dict(r)
        for r in await repo.fetchall(
            db,
            "SELECT pr_number, task_id, merge_sha FROM pipeline_merges "
            "WHERE merge_sha = ?",
            (RETURN_SHA,),
        )
    ]


@pytest.mark.asyncio
async def test_the_release_return_needs_no_role_bypass(db) -> None:
    # AC-1. Два тика поллера, как в жизни. Первый вливает релиз и открывает
    # PR возврата. Второй вливает PR возврата по зелёному CI. Прямой мерж
    # веток (POST /merges) не звучит ни разу — ему и нужен был обход.
    g = _git()
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, reason
    g.open_release_pr.assert_not_awaited()
    g.open_return_pr.assert_awaited_once()
    call = g.open_return_pr.await_args
    assert call.args[0] == "develop", "PR возврата вливается в интеграционную"
    assert call.args[1] == "main", "...из релизной ветки, а не наоборот"
    assert f"PR #{RETURN_PR}" in reason, f"возврат виден в отчёте: {reason!r}"
    g.merge_return_pr.assert_not_awaited()  # CI у свежего PR ещё не прошёл

    # Второй тик: релизного PR больше нет, PR возврата зелёный.
    g.pr_for_branch.return_value = None
    g.content_differs.return_value = False
    g.open_pr_between.return_value = (RETURN_PR, "")

    merged, reason = await merge_ready_release(db, project)

    assert (merged, reason) == (False, ""), "влитый возврат — не причина стоять"
    g.open_pr_between.assert_awaited()
    between = g.open_pr_between.await_args
    assert between.args[:2] == ("develop", "main"), between
    g.merge_return_pr.assert_awaited_once()
    assert g.merge_return_pr.await_args.args[0] == RETURN_PR
    g.merge_branches.assert_not_awaited()
    g.return_release_into_base.assert_not_awaited()

    rows = await _return_rows(db)
    assert rows == [
        {"pr_number": RETURN_PR, "task_id": None, "merge_sha": RETURN_SHA}
    ], (
        "мерж PR возврата обязан лечь в pipeline_merges, иначе drift-guard "
        f"назовёт собственный мерж хаба посторонним: {rows}"
    )


@pytest.mark.asyncio
async def test_the_merged_return_is_recorded_for_the_drift_guard(db) -> None:
    # #534/#1343: drift-guard судит по SHA. Коммит на develop ожидаем, только
    # если хаб записал, что произвёл его сам.
    _git(release_pr=None, return_pr=RETURN_PR)
    project = await _project(db)

    await merge_ready_release(db, project)

    known = await repo.known_pipeline_shas(db, int(dict(project)["id"]))
    assert RETURN_SHA in known, f"SHA возврата не записан: {known}"
    activity = [dict(r) for r in await repo.list_activity(db, limit=10)]
    assert any(f"PR #{RETURN_PR}" in (a.get("summary") or "") for a in activity), (
        activity
    )


@pytest.mark.asyncio
async def test_red_ci_on_the_return_does_not_stop_the_release(db) -> None:
    # Красный CI на PR возврата — причина, названная рядом с релизом, а не
    # вместо него. Релиз вливается, возврат ждёт починки.
    g = _git(return_pr=RETURN_PR, return_ci=CIProbeOutcome.failed)
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, f"красный CI возврата уронил релиз: {reason!r}"
    g.merge_pr.assert_awaited()
    g.merge_return_pr.assert_not_awaited()
    assert f"PR #{RETURN_PR}" in reason and "ci_fail" in reason, reason
    assert not await _return_rows(db), "невлитый возврат записан как влитый"


@pytest.mark.asyncio
async def test_red_ci_on_the_return_is_named_without_a_release(db) -> None:
    # Релиза в этом тике нет, а возврат стоит на красном. Молчание здесь
    # означало бы расхождение, которое снова копится беззвучно (#725).
    g = _git(release_pr=None, return_pr=RETURN_PR, return_ci=CIProbeOutcome.failed)
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert f"PR #{RETURN_PR}" in reason and "ci_fail" in reason, reason
    g.merge_return_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_conflicting_return_is_named_and_the_release_goes_on(db) -> None:
    # Конфликт назван файлами, которые показал git (#970), и релиз проходит.
    g = _git(
        return_pr=RETURN_PR,
        return_mergeable=(
            MergeabilityOutcome.conflicting,
            "конфликт с базовой веткой: hub/db.py",
        ),
    )
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, reason
    assert "hub/db.py" in reason and f"PR #{RETURN_PR}" in reason, reason
    g.merge_return_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_ci_on_the_return_is_silent(db) -> None:
    # Свежий PR возврата с идущим CI — обычное состояние, а не новость.
    # Поллер проходит здесь каждый цикл, и строка на цикл глушит настоящий
    # сигнал (#534).
    g = _git(release_pr=None, return_pr=RETURN_PR, return_ci=CIProbeOutcome.pending)
    project = await _project(db)

    assert await merge_ready_release(db, project) == (False, "")
    g.merge_return_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_return_merge_is_named(db) -> None:
    _git(release_pr=None, return_pr=RETURN_PR, return_merge=(False, ""))
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert f"PR #{RETURN_PR}" in reason and "не влит" in reason, reason
    assert not await _return_rows(db)


@pytest.mark.asyncio
async def test_a_return_pr_that_could_not_be_opened_is_named(db) -> None:
    _git(opened_return=None)
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, "релиз состоялся, что бы ни было с возвратом"
    assert "не открыт" in reason, reason
    activity = [dict(r) for r in await repo.list_activity(db, limit=10)]
    assert any("возврат" in (a.get("summary") or "").lower() for a in activity), (
        activity
    )


# ---------------------------------------------------------------------------
# Уровень форжа: мерж-коммит, голова цела, прямого мержа веток нет
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_return_is_merged_as_a_merge_commit_keeping_main() -> None:
    # Squash вернул бы в develop новый коммит, а коммит релиза так и не стал бы
    # предком develop — расхождение #969 копилось бы дальше. Удалить голову
    # значит удалить main.
    from hub.integrations.forge.github import GitHubForge
    from hub.integrations.git_ops import GitOpsIntegration

    with (
        patch(
            "hub.integrations.forge.github._gh",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_gh,
        patch.object(
            GitHubForge, "merge_commit_sha", AsyncMock(return_value=RETURN_SHA)
        ),
    ):
        ok, sha = await GitOpsIntegration().merge_return_pr(
            RETURN_PR,
            "chore: return main into develop after the release",
            repo="/ws/hub",
            gh_repo="agentdrover/haiplane",
        )

    assert (ok, sha) == (True, RETURN_SHA)
    calls = [list(c.args) for c in mock_gh.await_args_list]
    merge = next(c for c in calls if c[:2] == ["pr", "merge"])
    assert "--merge" in merge and "--squash" not in merge, merge
    assert "--delete-branch" not in merge, "голова PR возврата — main"
    assert not any(any("/merges" in str(a) for a in c) for c in calls), calls


@pytest.mark.asyncio
async def test_a_failed_return_lookup_is_named_not_read_as_no_pr(db) -> None:
    # #516: «не смог прочитать список PR» и «PR возврата нет» ведут к разным
    # действиям. Сбой чтения — причина, а не тишина.
    g = _git(release_pr=None, return_lookup_failure="список PR не прочитан: 502")
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "502" in reason and "возврат" in reason, reason
    g.merge_return_pr.assert_not_awaited()


def _gh_listing(prs: list[dict] | None, *, fail: bool = False):
    """``_gh`` для GitHubForge: отвечает на ``pr list``, записывает ``pr create``."""
    calls: list[list[str]] = []

    async def fake(*args, **_kw):
        calls.append([str(a) for a in args])
        if args[:2] == ("pr", "list"):
            if fail:
                return (1, "", "HTTP 502")
            return (0, json.dumps(prs or []), "")
        if args[:2] == ("pr", "create"):
            return (0, f"https://github.com/agentdrover/haiplane/pull/{RETURN_PR}", "")
        return (0, "", "")

    return fake, calls


def _pr(number: int, *, cross: bool = False, owner: str = "agentdrover") -> dict:
    return {
        "number": number,
        "isCrossRepository": cross,
        "headRepositoryOwner": {"login": owner},
    }


@pytest.mark.asyncio
async def test_a_fork_pr_from_main_into_develop_is_not_the_return() -> None:
    # Находка deep-ревью №1. ``--head main`` фильтрует только имя ветки, и под
    # него попадает attacker:main из форка. CI для PR из форка идёт, PR
    # зелёный — и без проверки он вливался бы в develop мерж-коммитом.
    from hub.integrations.git_ops import GitOpsIntegration

    fake, calls = _gh_listing(
        [_pr(66, cross=True, owner="attacker"), _pr(67, owner="attacker")]
    )
    with patch("hub.integrations.forge.github._gh", side_effect=fake):
        found, why = await GitOpsIntegration().open_pr_between(
            "develop", "main", gh_repo="agentdrover/haiplane"
        )

    assert found is None, "PR из чужого репозитория принят за возврат"
    assert "#66" in why and "#67" in why, f"пропуск обязан быть назван: {why!r}"
    listing = next(c for c in calls if c[:2] == ["pr", "list"])
    assert listing[listing.index("--base") + 1] == "develop", listing
    assert listing[listing.index("--head") + 1] == "main", listing


@pytest.mark.asyncio
async def test_a_fork_pr_is_never_merged_as_the_return(db) -> None:
    # Та же находка на уровне релиза: зелёный PR из форка не вливается.
    from hub.integrations.git_ops import GitOpsIntegration

    g = _git(release_pr=None)
    real = GitOpsIntegration()
    g.open_pr_between = real.open_pr_between
    project = await _project(db)

    fake, _ = _gh_listing([_pr(66, cross=True, owner="attacker")])
    with patch("hub.integrations.forge.github._gh", side_effect=fake):
        merged, reason = await merge_ready_release(db, project)

    assert merged is False
    g.merge_return_pr.assert_not_awaited()
    assert "#66" in reason, reason


@pytest.mark.asyncio
async def test_the_own_repo_pr_is_the_return_even_next_to_a_fork_pr() -> None:
    from hub.integrations.git_ops import GitOpsIntegration

    fake, _ = _gh_listing([_pr(66, cross=True, owner="attacker"), _pr(RETURN_PR)])
    with patch("hub.integrations.forge.github._gh", side_effect=fake):
        found, why = await GitOpsIntegration().open_pr_between(
            "develop", "main", gh_repo="agentdrover/haiplane"
        )

    assert (found, why) == (RETURN_PR, "")


@pytest.mark.asyncio
async def test_opening_the_return_leaves_a_pr_into_another_base_alone() -> None:
    # Находка deep-ревью №2. Уже открыт PR main → staging. Старый путь
    # (open_or_update_pr) нашёл бы его по голове, переименовал в «возврат» и
    # не создал бы main → develop. Возврат ищется по паре база+голова.
    from hub.integrations.git_ops import GitOpsIntegration

    state = {"created": False}

    async def fake(*args, **_kw):
        if args[:2] == ("pr", "list"):
            assert "--base" in args, f"PR возврата ищется без базы: {args}"
            listed = [_pr(RETURN_PR)] if state["created"] else []
            return (0, json.dumps(listed), "")
        if args[:2] == ("pr", "create"):
            state["created"] = True
            return (0, f"https://github.com/agentdrover/haiplane/pull/{RETURN_PR}", "")
        return (0, "", "")

    with patch("hub.integrations.forge.github._gh", side_effect=fake) as gh:
        pr, why = await GitOpsIntegration().open_return_pr(
            "develop",
            "main",
            "chore: return main into develop after the release",
            "body",
            gh_repo="agentdrover/haiplane",
        )

    assert (pr, why) == (RETURN_PR, "")
    calls = [[str(a) for a in c.args] for c in gh.await_args_list]
    assert not any(c[:2] == ["pr", "edit"] for c in calls), "чужой PR переименован"
    create = next(c for c in calls if c[:2] == ["pr", "create"])
    assert create[create.index("--base") + 1] == "develop", create
    assert create[create.index("--head") + 1] == "main", create


@pytest.mark.asyncio
async def test_a_fork_pr_does_not_stop_the_own_return_from_being_opened() -> None:
    # Неразрешённая находка сдачи 2: открытый PR attacker:main → develop не
    # должен блокировать возврат. Чужой PR не трогается, свой создаётся.
    from hub.integrations.git_ops import GitOpsIntegration

    state = {"created": False}

    async def fake(*args, **_kw):
        if args[:2] == ("pr", "list"):
            listed = [_pr(66, cross=True, owner="attacker")]
            if state["created"]:
                listed.append(_pr(RETURN_PR))
            return (0, json.dumps(listed), "")
        if args[:2] == ("pr", "create"):
            state["created"] = True
            return (0, f"https://github.com/agentdrover/haiplane/pull/{RETURN_PR}", "")
        return (0, "", "")

    with patch("hub.integrations.forge.github._gh", side_effect=fake) as gh:
        pr, why = await GitOpsIntegration().open_return_pr(
            "develop", "main", "t", "b", gh_repo="agentdrover/haiplane"
        )

    assert (pr, why) == (RETURN_PR, ""), why
    calls = [[str(a) for a in c.args] for c in gh.await_args_list]
    assert not any(c[:2] == ["pr", "edit"] for c in calls), "чужой PR тронут"


@pytest.mark.asyncio
async def test_gitverse_does_not_take_a_pr_without_head_repo_for_its_own() -> None:
    # Неразрешённая находка сдачи 2: у GitVerse нет head.repo (удалённый форк,
    # урезанный ответ) — голова не доказана своей, PR не принимается.
    from hub.integrations.forge.gitverse import GitVerseForge, GitVerseResponse

    forge = GitVerseForge(token="t", base_url="https://api.example", version="1")
    listed = [
        {"number": 66, "base": {"ref": "develop"}, "head": {"ref": "main"}},
        {
            "number": 67,
            "base": {"ref": "develop"},
            "head": {"ref": "main", "repo": None},
        },
    ]
    with patch.object(
        forge, "_request", AsyncMock(return_value=GitVerseResponse(200, listed))
    ):
        found, why = await forge.pr_between(
            "develop", "main", gh_repo="agentdrover/haiplane"
        )

    assert found is None, "PR без head.repo принят за свой"
    assert "#66" in why and "#67" in why, why


@pytest.mark.asyncio
async def test_a_failed_listing_is_a_reason_not_no_pr() -> None:
    # Неразрешённая находка: сбой чтения нельзя свести к «PR нет» (#516) —
    # иначе открытие возврата пошло бы создавать второй PR вслепую.
    from hub.integrations.git_ops import GitOpsIntegration

    fake, calls = _gh_listing(None, fail=True)
    with patch("hub.integrations.forge.github._gh", side_effect=fake):
        found, why = await GitOpsIntegration().open_pr_between(
            "develop", "main", gh_repo="agentdrover/haiplane"
        )
        pr, open_why = await GitOpsIntegration().open_return_pr(
            "develop", "main", "t", "b", gh_repo="agentdrover/haiplane"
        )

    assert found is None and "502" in why, why
    assert pr is None and "502" in open_why, open_why
    assert not any(c[:2] == ["pr", "create"] for c in calls), "создан вслепую"


@pytest.mark.asyncio
async def test_the_noop_integration_cannot_return_and_says_so() -> None:
    g = NoopGitOps()
    assert await g.open_pr_between("develop", "main") == (None, "")
    pr, why = await g.open_return_pr("develop", "main", "t", "b")
    assert pr is None and why
    ok, why = await g.merge_return_pr(RETURN_PR, "subject")
    assert ok is False and why
