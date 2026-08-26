"""Релиз возвращает релизную ветку в интеграционную (#969).

26.08.2026 релизный PR #83 (develop → main) встал в конфликт при зелёном CI:
mergeable=CONFLICTING, mergeStateStatus=DIRTY. Прод не получил 13 задач.

Причина не в конфликте, а в том, что его накопили. Squash-релиз кладёт в
релизную ветку НОВЫЙ коммит, которого нет в истории интеграционной, и обратно
он не возвращается никогда. После каждого релиза main опережает develop ровно
на один такой коммит; к PR #83 их набралось пять подряд. Расхождение безвредно
ровно до того момента, когда git не сможет свести хвосты — и тогда встаёт весь
конвейер.

Это был второй случай за двадцать часов: первый расшит вручную в PR #36
(26.08, 00:36Z), второй — в PR #85. Одинаковая ручная операция дважды за сутки
— пропущенный шаг конвейера, а не стечение обстоятельств.

Момент возврата выбран не произвольно: сразу после мержа релиза squash-коммит
имеет РОВНО то же дерево, что вершина интеграционной ветки, а merge-base
свежий — сливать нечего и конфликтовать не с чем. Дальше расхождение только
стареет.

Развилка «squash против мерж-коммита» здесь НЕ пересматривается: она решена в
#946 в пользу squash, потому что линейная main была осознанным выбором
постановки #927. Возврат даёт тот же результат, не отменяя того решения.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import MergeabilityOutcome
from hub.integrations.registry import plugins
from hub.services.release import merge_ready_release


def _git_after_a_release(back_merge: tuple[str, str]):
    """Плагин git, у которого релиз уже прошёл, а возврат отвечает ``back_merge``.

    Объявляет КАЖДЫЙ вопрос, который задаёт релизный путь. Половинчатый фейк —
    ровно та ловушка, на которой #968 потерял семь тестов: незаявленный метод
    уходит в noop, и любой случай читается как «не смог посмотреть».
    """
    g = NoopGitOps()
    from hub.integrations.protocols import CIProbeOutcome, CIProbeResult

    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    g.pr_for_branch = AsyncMock(return_value=83)
    g.merge_pr = AsyncMock(return_value=True)
    g.merge_commit_sha = AsyncMock(return_value="c" * 40)
    g.content_differs = AsyncMock(return_value=True)
    # #970: зелёный CI перестал быть разрешением мержить — релиз отдельно
    # спрашивает, сливается ли PR. Здесь релиз должен состояться, значит
    # ответ утвердительный; оставить вопрос noop'у значило бы сделать все
    # тесты этого файла проверкой отказа вместо проверки возврата.
    g.check_pr_mergeable = AsyncMock(
        return_value=(MergeabilityOutcome.mergeable, "clean")
    )
    g.ensure_remote_branch = AsyncMock(return_value=("present", "b" * 12))
    g.return_release_into_base = AsyncMock(return_value=back_merge)
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


@pytest.mark.asyncio
async def test_release_returns_base_into_the_integration_branch(db) -> None:
    # AC-1: релиз смержен — релизная ветка немедленно возвращена в
    # интеграционную и запушена. Без этого шага расхождение начинает копиться
    # с первого же релиза, и следующий встаёт в конфликт, как PR #83.
    g = _git_after_a_release(("returned", "a" * 40))
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, reason
    g.return_release_into_base.assert_awaited()
    call = g.return_release_into_base.await_args
    assert call.args[0] == "main", "возвращаем релизную ветку..."
    assert call.args[1] == "develop", "...в интеграционную, а не наоборот"
    assert "возвращ" in reason, f"возврат должен быть виден в отчёте: {reason!r}"


@pytest.mark.asyncio
async def test_the_returned_merge_is_recorded_as_the_hubs_own(db) -> None:
    # AC-4: возврат пушит в интеграционную ветку от имени хаба. Drift-guard
    # судит по SHA — коммит на базовой ветке ожидаем только тогда, когда его
    # SHA записан как собственный мерж хаба (#534). Незаписанный возврат
    # означал бы алерт о постороннем мерже в develop на КАЖДОМ релизе: мы бы
    # заменили ручное нарушение правила на автоматическое.
    _git_after_a_release(("returned", "a" * 40))
    project = await _project(db)

    await merge_ready_release(db, project)

    known = await repo.known_pipeline_shas(db, int(dict(project)["id"]))
    assert "a" * 40 in known, (
        "SHA возврата обязан попасть в pipeline_merges, иначе drift-guard "
        f"назовёт собственный мерж хаба посторонним: {known}"
    )


@pytest.mark.asyncio
async def test_nothing_to_return_stays_silent(db) -> None:
    # AC-2: возвращать нечего — молчание. Поллер проходит здесь каждый цикл, и
    # строка на цикл — это то, как глушат настоящий сигнал (#534).
    _git_after_a_release(("nothing", "main уже в develop"))
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "возвращ" not in reason, f"нечего возвращать — не новость: {reason!r}"
    activity = [dict(r) for r in await repo.list_activity(db, limit=10)]
    assert not any("возврат" in (a.get("summary") or "").lower() for a in activity), (
        activity
    )


@pytest.mark.asyncio
async def test_a_conflicting_back_merge_names_the_conflict(db) -> None:
    # AC-3, первая половина: конфликт назван, а не проглочен, и назван так,
    # чтобы человек знал, куда идти.
    _git_after_a_release(("conflict", "конфликт в hub/db.py"))
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True, "релиз состоялся — код в проде, что бы ни было потом"
    assert "hub/db.py" in reason, f"причина обязана назвать конфликт: {reason!r}"
    activity = [dict(r) for r in await repo.list_activity(db, limit=10)]
    assert any(
        "hub/db.py" in (a.get("detail") or "") + (a.get("summary") or "")
        for a in activity
    ), f"конфликт возврата должен дойти до человека, а не до лога: {activity}"


@pytest.mark.asyncio
async def test_failed_back_merge_is_named_not_swallowed(db) -> None:
    # AC-3, вторая половина: «не смог посмотреть» — тоже причина, а не тишина
    # (#725). Молча не вернувшийся возврат означает расхождение, которое снова
    # копится, но теперь уже беззвучно — а молчащую поломку ищут дольше
    # шумящей.
    _git_after_a_release(("unavailable", "gh не ответил"))
    project = await _project(db)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "не ответил" in reason or "не проверен" in reason, reason
    activity = [dict(r) for r in await repo.list_activity(db, limit=10)]
    assert any("возврат" in (a.get("summary") or "").lower() for a in activity), (
        f"неизвестность обязана быть видимой: {activity}"
    )


@pytest.mark.asyncio
async def test_a_failed_back_merge_does_not_fail_the_release(db) -> None:
    # AC-3, граница: релиз, который состоялся, остаётся состоявшимся. Если
    # неудачу возврата принять за неудачу релиза, задача уедет в
    # fix_requested, хотя её код уже раскатан — ровно тот класс лжи о проде,
    # ради которого написан эпик #499.
    g = _git_after_a_release(("conflict", "конфликт в hub/db.py"))
    project = await _project(db)

    merged, _ = await merge_ready_release(db, project)

    assert merged is True
    g.merge_pr.assert_awaited(), "релиз должен был пройти до возврата"


@pytest.mark.asyncio
async def test_the_return_happens_after_the_release_is_stamped(db) -> None:
    # Порядок: штамп раскатки (#950) считает НЕраскатанные мержи гейта, и
    # возврат, записанный раньше штампа, был бы объявлен уехавшим этим
    # релизом. Он уедет следующим — это и есть правда.
    _git_after_a_release(("returned", "a" * 40))
    project = await _project(db)

    await merge_ready_release(db, project)

    rows = [
        dict(r)
        for r in await repo.fetchall(
            db,
            "SELECT merge_sha, released_sha FROM pipeline_merges WHERE merge_sha = ?",
            ("a" * 40,),
        )
    ]
    assert rows, "возврат обязан быть записан"
    assert not (rows[0]["released_sha"] or ""), (
        "возврат не уехал этим релизом — он уедет следующим, и штамп обязан "
        f"это отражать: {rows[0]}"
    )


# ---------------------------------------------------------------------------
# Имена конфликтующих файлов — на настоящем git, а не на пересказе мока
# ---------------------------------------------------------------------------


def _repo_with_a_real_conflict(tmp_path) -> str:
    """Клон, где main и develop разошлись так же, как 26.08: хвост одного файла.

    Ровно форма PR #83: обе ветки дописали в конец одного и того же списка,
    merge-base старый. Настоящий git, потому что вопрос ровно в том, что
    ответит git — пересказ мока проверял бы наши представления о нём.
    """
    import subprocess
    from pathlib import Path

    def _run(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=False, capture_output=True
        )

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(seed, "init", "-q", "-b", "main")
    _run(seed, "config", "user.email", "t@example.com")
    _run(seed, "config", "user.name", "T")
    (seed / "db.py").write_text("MIGRATIONS = [\n    'base',\n]\n")
    _run(seed, "add", "-A")
    _run(seed, "commit", "-qm", "base")
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    _run(seed, "remote", "add", "origin", str(remote))
    _run(seed, "push", "-q", "--no-verify", "origin", "main")

    _run(seed, "checkout", "-q", "-b", "develop")
    (seed / "db.py").write_text("MIGRATIONS = [\n    'base',\n    'develop side',\n]\n")
    _run(seed, "commit", "-qam", "develop dopisal")
    _run(seed, "push", "-q", "--no-verify", "origin", "develop")

    _run(seed, "checkout", "-q", "main")
    (seed / "db.py").write_text("MIGRATIONS = [\n    'base',\n    'main side',\n]\n")
    _run(seed, "commit", "-qam", "main dopisal")
    _run(seed, "push", "-q", "--no-verify", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True
    )
    return str(clone)


@pytest.mark.asyncio
async def test_a_conflict_is_named_with_the_files_git_points_at(tmp_path) -> None:
    # AC-3: причина обязана вести человека к месту. «Конфликт» без файла —
    # это приглашение лезть в GitHub глазами, ровно то, чего задача избегает.
    from hub.integrations.git_ops import GitOpsIntegration

    clone = _repo_with_a_real_conflict(tmp_path)

    files = await GitOpsIntegration()._conflicting_files("main", "develop", repo=clone)

    assert "db.py" in files, f"git показывает конфликт в db.py, а мы — {files}"


@pytest.mark.asyncio
async def test_no_clone_names_no_files_and_claims_nothing(tmp_path) -> None:
    # Граница того же: пустой список означает «назвать не смог», а НЕ «файлов
    # не было». Конфликт остаётся конфликтом и докладывается без имён (#725).
    from hub.integrations.git_ops import GitOpsIntegration

    assert await GitOpsIntegration()._conflicting_files("main", "develop") == []
