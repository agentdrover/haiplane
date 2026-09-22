"""Отказ «предмета нет в базовой ветке» обязан говорить правду (#1287).

Проверка предмета (#1232) остаётся той же и по тому же правилу: присутствием
считается ОПРЕДЕЛЕНИЕ, а не текстовое совпадение — иначе имя в комментарии
откроет пустую задачу. Здесь проверяется другое: что именно отказ УТВЕРЖДАЕТ
про имена, которых он не нашёл.

Наблюдено 22.09.2026 на #1282: отказ назвал отсутствующими в develop пять
имён, а отсутствовало одно. У трёх определения лежали в файлах, которых
проверка не открывала — она разбирает только .py из affected_areas. Четвёртое,
steward_judgements, — имя таблицы: определения у него нет нигде, но в базовой
ветке оно есть.

Тесты #1232 про сам гейт живут в tests/test_readiness.py; здесь — только
честность отказа.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins

DECLARED_AREA = "hub/services/declared.py"
ELSEWHERE = "hub/services/elsewhere.py"
STORE = "hub/db.py"
BROKEN = "hub/services/broken.py"
OTHER_BRANCH = "task-9002/carries-the-missing-name"

# Объявленная область: про предмет она не знает ничего.
DECLARED_SOURCE = '''"""The one module the statement declares."""


def existing_helper() -> int:
    return 1
'''

# Файл, которого постановка НЕ называла, — и определение лежит именно тут.
# Это случай bump_if_the_statement_changed из #1282.
ELSEWHERE_SOURCE = '''"""A module nobody declared, carrying the real definition."""


def defined_far_away() -> str:
    return "here all along"
'''

# Имя таблицы и имя колонки: определения нет по построению, а в базе они есть.
# Это случай steward_judgements. Заодно ledger_rows — длинное слово, внутри
# которого целиком лежит ledger_row: подстрока присутствием быть не должна.
STORE_SOURCE = '''"""Schema, where names live as text and nothing else."""

MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS ledger_rows (id INTEGER PRIMARY KEY)",
    "ALTER TABLE ledger_rows ADD COLUMN settled_at TEXT NOT NULL DEFAULT ''",
)
'''

# Файл, который не разбирается. Имя в нём есть, а сказать про него нечего:
# «посмотреть не удалось» — не «есть только как текст» (#725).
BROKEN_SOURCE = '''"""This file does not parse."""

def unreadable_subject(
'''

# Что несёт ветка другой задачи: имя, которого в базе нет вовсе.
BRANCH_SOURCE = '''"""The module as the other task's branch leaves it."""


def existing_helper() -> int:
    return 1


def never_written_anywhere() -> str:
    return "only here"
'''


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(root),
        },
    ).stdout.strip()


def _clone(tmp_path: Path) -> str:
    """Клон, где базовая ветка знает три файла, а ветка задачи — четвёртое имя."""
    root = tmp_path / "clone"
    (root / "hub" / "services").mkdir(parents=True)
    _git(root, "init", "-b", "develop")
    (root / DECLARED_AREA).write_text(DECLARED_SOURCE)
    (root / ELSEWHERE).write_text(ELSEWHERE_SOURCE)
    (root / STORE).write_text(STORE_SOURCE)
    (root / BROKEN).write_text(BROKEN_SOURCE)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    _git(root, "checkout", "-q", "-b", OTHER_BRANCH)
    (root / DECLARED_AREA).write_text(BRANCH_SOURCE)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "the other task's work")
    _git(root, "checkout", "-q", "develop")
    return str(root)


def _use_real_git_reads(monkeypatch) -> None:
    """Читать файлы из настоящего репозитория; остальные git-операции — мок."""
    real = GitOpsIntegration()
    monkeypatch.setattr(plugins.git_ops, "file_at_ref", real.file_at_ref, raising=False)
    monkeypatch.setattr(
        plugins.git_ops, "files_at_ref", real.files_at_ref, raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "files_naming_at_ref",
        real.files_naming_at_ref,
        raising=False,
    )


async def _project_at(db: aiosqlite.Connection, workspace: str) -> int:
    return await repo.create_project(
        db,
        slug="subject-honesty",
        name="Subject honesty",
        workspace_path=workspace,
        default_branch="develop",
    )


async def _other_task(client: AsyncClient, db: aiosqlite.Connection) -> int:
    """Незавершённая задача, чья ветка несёт недостающее имя."""
    other_id = (await client.post("/api/tasks", json={"title": "Носитель"})).json()[
        "id"
    ]
    await db.execute(
        "UPDATE tasks SET status='review', branch=?, submission_sha='c0ffee' "
        "WHERE id=?",
        (OTHER_BRANCH, other_id),
    )
    await db.commit()
    return other_id


async def _task_named(
    client: AsyncClient, db: aiosqlite.Connection, project_id: int, hints: str
) -> int:
    task_id = (await client.post("/api/tasks", json={"title": "Задача"})).json()["id"]
    await repo.update_task(db, task_id, project_id=project_id)
    await db.commit()
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"technical_hints": hints, "affected_areas": [DECLARED_AREA]},
    )
    await client.post(f"/api/tasks/{task_id}/approve", json={"force": True})
    await client.post(f"/api/tasks/{task_id}/claim", json={"agent": "dev"})
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    return task_id


async def _pair_start(client: AsyncClient, task_id: int):
    return await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )


async def test_a_name_without_a_definition_is_not_called_missing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """#1287 AC-1: то, что в базе есть, отказ отсутствующим не называет.

    Наблюдённый 22.09 случай #1282 целиком: четыре имени из пяти были названы
    отсутствующими зря. Здесь те же две причины на одном отказе.

    ``defined_far_away`` определён в файле, которого постановка не называла, —
    и это ПРИСУТСТВИЕ: имя перестаёт быть недостающим вовсе.

    ``ledger_rows`` — имя таблицы: определения у него нет нигде, и предметом
    оно не становится (иначе имя в комментарии открывало бы пустую задачу,
    #1232). Но в базе оно есть, и отказ это говорит отдельной фразой вместо
    «не найдено в базе» — вместе с файлом, где смотреть.
    """
    workspace = _clone(tmp_path)
    _use_real_git_reads(monkeypatch)
    project_id = await _project_at(db, workspace)
    other_id = await _other_task(client, db)
    task_id = await _task_named(
        client,
        db,
        project_id,
        "Опереться на defined_far_away, ledger_rows и never_written_anywhere.",
    )

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "subject_not_in_base_branch"
    assert detail["found_in_task_id"] == other_id

    # Определение нашлось вне объявленных областей — имя не недостающее.
    assert "defined_far_away" not in detail["missing"]
    assert "defined_far_away" not in detail["text_only"]

    # Имя таблицы: предметом не стало, но и отсутствующим не названо.
    assert "ledger_rows" in detail["text_only"]

    hint = detail["hint"]
    absent = hint.split("вовсе:", 1)[1].split(".", 1)[0]
    assert "never_written_anywhere" in absent
    assert "ledger_rows" not in absent
    assert "defined_far_away" not in hint
    assert STORE in hint, "не сказано, где имя всё-таки есть"
    assert "СЛОВОМ ЦЕЛИКОМ" in hint, "не назван способ поиска"


async def test_a_truly_absent_subject_still_refuses(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """#1287 AC-2: отказ не ослаб — чего нет, то названо.

    Другой край той же правки. Расширение поиска обязано оставить отказ
    работающим ровно там, где предмет действительно живёт в чужой ветке:
    иначе честность куплена ценой самого гейта.
    """
    workspace = _clone(tmp_path)
    _use_real_git_reads(monkeypatch)
    project_id = await _project_at(db, workspace)
    other_id = await _other_task(client, db)
    task_id = await _task_named(
        client, db, project_id, "Починить never_written_anywhere."
    )

    resp = await _pair_start(client, task_id)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["missing"] == ["never_written_anywhere"]
    assert detail["text_only"] == []
    assert detail["found_in_branch"] == OTHER_BRANCH
    assert detail["found_in_task_id"] == other_id
    assert "never_written_anywhere" in detail["hint"]
    # Задача не открылась.
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "claimed"


async def test_a_substring_is_not_presence(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """#1287 AC-3: совпадение внутри другого слова присутствием не считается.

    ``ledger_row`` целиком лежит внутри ``ledger_rows``, которое в базе есть.
    Поиск подстрокой объявил бы имя найденным — и отказ сказал бы про него
    «есть в базе, только без определения», чего про него сказать нельзя:
    такого слова в базе нет. Мутация «искать подстрокой» (снять -w у git grep)
    роняет именно этот тест.
    """
    workspace = _clone(tmp_path)
    _use_real_git_reads(monkeypatch)
    project_id = await _project_at(db, workspace)
    await _other_task(client, db)
    task_id = await _task_named(
        client, db, project_id, "Завести ledger_row и never_written_anywhere."
    )

    from hub.services.readiness import SUBJECT_STRANDED, subject_presence

    row = await repo.get_task(db, task_id)
    presence = await subject_presence(db, dict(row))

    assert presence.verdict == SUBJECT_STRANDED
    assert "ledger_row" in presence.missing
    assert "ledger_row" not in presence.text_only, (
        "подстрока внутри ledger_rows названа присутствием в базе"
    )
    assert "ledger_row" in presence.reason.split("вовсе:", 1)[1].split(".", 1)[0]


async def test_a_file_that_would_not_parse_is_not_called_text_only(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path: Path
):
    """«Посмотреть не удалось» — своё утверждение, а не одно из двух других.

    Имя ``unreadable_subject`` встречается в базовой ветке ровно в одном файле,
    и этот файл не разбирается. Сказать «есть, но без определения» про него
    нельзя: определения там, может быть, как раз и есть. Отказ обязан назвать
    неизвестность неизвестностью — то же правило #725, по которому «не смогли
    посмотреть» никогда не печатается как «посмотрели и нет».
    """
    workspace = _clone(tmp_path)
    _use_real_git_reads(monkeypatch)
    project_id = await _project_at(db, workspace)
    await _other_task(client, db)
    task_id = await _task_named(
        client,
        db,
        project_id,
        "Опереться на unreadable_subject и never_written_anywhere.",
    )

    from hub.services.readiness import subject_presence

    row = await repo.get_task(db, task_id)
    presence = await subject_presence(db, dict(row))

    assert "unreadable_subject" in presence.missing
    assert "unreadable_subject" not in presence.text_only
    assert "посмотреть не удалось" in presence.reason
    assert (
        "unreadable_subject"
        not in presence.reason.split("вовсе:", 1)[1].split(".", 1)[0]
    )
