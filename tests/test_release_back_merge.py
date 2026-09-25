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

Сам возврат с #1426 идёт PR-ом, а не прямым мержем веток; его проверки — в
tests/test_release.py. Здесь остались имена конфликтующих файлов на настоящем
git: их по-прежнему читает check_pr_mergeable, в том числе для PR возврата.
"""

from __future__ import annotations


import pytest


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
