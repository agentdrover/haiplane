"""Миграции по одному файлу (#1854): загрузчик steps/, порядок доставки, add-only."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

import hub.db as db_module
from hub.db import _SCHEMA, _migrate
from hub.migrations import FROZEN_MIGRATIONS
from hub.migrations.steps import (
    MigrationStepError,
    assemble,
    load_step_files,
    load_steps,
    stray_entries,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from migration_steps_add_only import ci_base, main as add_only_main  # noqa: E402
from migration_steps_add_only import violations  # noqa: E402

STEP_A = "m202610190900_9001_probe_a.py"
STEP_B = "m202610191000_9002_probe_b.py"

_A_SRC = (
    "MIGRATIONS = [\n"
    '    ("probe_a_column", "ALTER TABLE tasks ADD COLUMN probe_a TEXT"),\n'
    '    ("probe_a_index", "CREATE INDEX idx_probe_a ON tasks(probe_a)"),\n'
    "]\n"
)
_B_SRC = (
    "MIGRATIONS = [\n"
    '    ("probe_b_table", "CREATE TABLE probe_b (id INTEGER PRIMARY KEY, v TEXT)"),\n'
    "]\n"
)


def _write(directory: Path, name: str, source: str) -> None:
    (directory / name).write_text(source, encoding="utf-8")


def _steps_dir(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "steps"
    directory.mkdir()
    for name, source in files.items():
        _write(directory, name, source)
    return directory


def test_steps_are_discovered_in_file_order(tmp_path):
    # Записаны B, потом A: порядок задают имена файлов, а не порядок создания.
    directory = tmp_path / "steps"
    directory.mkdir()
    _write(directory, STEP_B, _B_SRC)
    _write(directory, STEP_A, _A_SRC)
    (directory / "__pycache__").mkdir()

    files = load_step_files(directory, strict=True)
    assert [f.name for f in files] == [STEP_A, STEP_B]
    # Прочитано именно из переданного каталога: путь и содержимое из tmp_path.
    for f in files:
        assert f.path == directory / f.name
        assert f.path.read_text(encoding="utf-8") in (_A_SRC, _B_SRC)
    assert files[1].migrations[0][0] == "probe_b_table"

    assembled = assemble(FROZEN_MIGRATIONS, directory)
    assert assembled[: len(FROZEN_MIGRATIONS)] == FROZEN_MIGRATIONS
    assert [n for n, _ in assembled[len(FROZEN_MIGRATIONS) :]] == [
        "probe_a_column",
        "probe_a_index",
        "probe_b_table",
    ]


@pytest.mark.parametrize(
    "bad_name",
    [
        "migration.py",
        "m20261019_1_x.py",  # короткая метка времени
        "m202610191200_1854.py",  # нет «сути»
        "M202610191200_1854_x.py",
        "m202610191200_1854_x.v2.py",  # точка в имени
        "m202610191200_1854_x.txt",
    ],
)
def test_bad_file_name_is_strict_error_but_startup_skips_it(tmp_path, caplog, bad_name):
    directory = _steps_dir(tmp_path, **{STEP_A: _A_SRC})
    # Тело файла упало бы при исполнении: имя обязано отсечь его раньше чтения.
    _write(directory, bad_name, 'raise SystemExit("imported")')
    with pytest.raises(MigrationStepError, match="посторонние записи"):
        load_steps(directory, strict=True)
    with caplog.at_level("WARNING", logger="hub.migrations.steps"):
        assert [n for n, _ in load_steps(directory)] == [
            "probe_a_column",
            "probe_a_index",
        ]
    assert bad_name in caplog.text


@pytest.mark.parametrize(
    "source, message",
    [
        ("# пусто\n", "MIGRATIONS отсутствует"),
        ("X = 1\n", "допустимо только литеральное"),
        ('MIGRATIONS = (("a", "SELECT 1"),)\n', "не list"),
        ('MIGRATIONS = ["a"]\n', "кортеж"),
        ('MIGRATIONS = [("a", 1)]\n', "кортеж"),
        ('MIGRATIONS = [("a", "SELECT 1", "x")]\n', "кортеж"),
        ("from hub.db import _SCHEMA\nMIGRATIONS = []\n", "допустимо только"),
        ("import hub\nMIGRATIONS = []\n", "допустимо только"),
        ("from . import x\nMIGRATIONS = []\n", "допустимо только"),
        ("import importlib\nMIGRATIONS = []\n", "допустимо только"),
        ('MIGRATIONS = [__import__("hub.db")]\n', "не литерал"),
        ('MIGRATIONS = [("a", __import__("os").name)]\n', "не литерал"),
        ('MIGRATIONS = [("a", "SELECT " + "1")]\n', "не литерал"),
        ('MIGRATIONS = [("a", f"SELECT {1}")]\n', "не литерал"),
        ('__import__("hub.db")\nMIGRATIONS = []\n', "допустимо только"),
        ("MIGRATIONS = []\nMIGRATIONS = []\n", "допустимо только"),
        ("MIGRATIONS = [\n", "не разбирается"),
    ],
)
def test_bad_step_content_fails(tmp_path, source, message):
    directory = _steps_dir(tmp_path, **{STEP_A: source})
    with pytest.raises(MigrationStepError, match=message):
        load_steps(directory)


def test_duplicate_migration_name_fails(tmp_path):
    frozen_name = FROZEN_MIGRATIONS[0][0]
    clash = f'MIGRATIONS = [("{frozen_name}", "SELECT 1")]\n'
    with pytest.raises(MigrationStepError, match="дубль"):
        assemble(FROZEN_MIGRATIONS, _steps_dir(tmp_path, **{STEP_A: clash}))

    other = tmp_path / "other"
    other.mkdir()
    dup_src = 'MIGRATIONS = [("same", "SELECT 1")]\n'
    _write(other, STEP_A, dup_src)
    _write(other, STEP_B, dup_src)
    with pytest.raises(MigrationStepError, match="дубль"):
        assemble(FROZEN_MIGRATIONS, other)


def test_empty_or_missing_steps_dir_adds_nothing(tmp_path):
    assert assemble(FROZEN_MIGRATIONS, tmp_path / "missing") == FROZEN_MIGRATIONS
    assert assemble(FROZEN_MIGRATIONS, _steps_dir(tmp_path)) == FROZEN_MIGRATIONS


def test_real_steps_directory_is_valid_and_a_package():
    # Каталог - пакет (попадает в wheel/деплой) и проходит собственный загрузчик.
    steps_dir = REPO_ROOT / "hub" / "migrations" / "steps"
    assert (steps_dir / "__init__.py").is_file()
    assert db_module._MIGRATIONS == assemble(FROZEN_MIGRATIONS)
    load_steps()


# --- AC-3: порядок доставки ---------------------------------------------------


async def _fresh(migrations) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_SCHEMA)
    db_module._MIGRATIONS = migrations
    await _migrate(conn)
    return conn


async def _structure(conn: aiosqlite.Connection) -> dict:
    """Таблицы, колонки с типами и индексы - без байтов sqlite_master.sql."""
    tables = [
        r[0]
        for r in await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    columns = {}
    for table in tables:
        info = await conn.execute_fetchall(f"PRAGMA table_info({table})")
        columns[table] = sorted((r[1], r[2], r[3], r[5]) for r in info)
    indexes = {}
    for table in tables:
        found = []
        for r in await conn.execute_fetchall(f"PRAGMA index_list({table})"):
            cols = await conn.execute_fetchall(f"PRAGMA index_info({r[1]})")
            found.append((r[1], r[2], tuple(c[2] for c in cols)))
        indexes[table] = sorted(found)
    return {"columns": columns, "indexes": indexes}


async def test_steps_apply_in_any_delivery_order(tmp_path, monkeypatch):
    directory = tmp_path / "steps"
    directory.mkdir()
    _write(directory, STEP_A, _A_SRC)
    _write(directory, STEP_B, _B_SRC)
    full = assemble(FROZEN_MIGRATIONS, directory)
    a_only = [*FROZEN_MIGRATIONS, *load_steps(directory)[:2]]
    b_only = [*FROZEN_MIGRATIONS, *load_steps(directory)[2:]]
    assert [n for n, _ in b_only[len(FROZEN_MIGRATIONS) :]] == ["probe_b_table"]
    monkeypatch.setattr(db_module, "_MIGRATIONS", full)  # восстановление после теста

    opened: list[aiosqlite.Connection] = []
    try:
        # 1. части -> A -> B (по очереди, как приходили в ветку)
        c1 = await _fresh(list(FROZEN_MIGRATIONS))
        opened.append(c1)
        db_module._MIGRATIONS = a_only
        await _migrate(c1)
        db_module._MIGRATIONS = full
        await _migrate(c1)
        # 2. части -> B, потом поздно пришёл A с более ранним именем
        c2 = await _fresh(list(FROZEN_MIGRATIONS))
        opened.append(c2)
        db_module._MIGRATIONS = b_only
        await _migrate(c2)
        db_module._MIGRATIONS = full
        await _migrate(c2)
        # 3. свежая база со всем
        c3 = await _fresh(full)
        opened.append(c3)

        s1, s2, s3 = await _structure(c1), await _structure(c2), await _structure(c3)
        assert "probe_b" in s3["columns"]
        assert ("probe_a", "TEXT", 0, 0) in s3["columns"]["tasks"]
        assert s1 == s2 == s3

        # 4. повтор: ни SQL миграций, ни меток, ни данных
        await c2.execute("INSERT INTO probe_b (v) VALUES ('keep')")
        await c2.commit()
        marks = await c2.execute_fetchall("SELECT name, applied_at FROM _migrations")
        rows = await c2.execute_fetchall("SELECT id, v FROM probe_b")
        executed: list[str] = []
        real_execute = c2.execute

        async def spy(sql, *args, **kwargs):
            executed.append(sql)
            return await real_execute(sql, *args, **kwargs)

        c2.execute = spy  # type: ignore[method-assign]
        try:
            await _migrate(c2)
        finally:
            c2.execute = real_execute  # type: ignore[method-assign]
        migration_sql = {sql for _, sql in full}
        assert not [s for s in executed if s in migration_sql]
        assert (
            await c2.execute_fetchall("SELECT name, applied_at FROM _migrations")
            == marks
        )
        assert await c2.execute_fetchall("SELECT id, v FROM probe_b") == rows
        assert await _structure(c2) == s3
    finally:
        for conn in opened:
            await conn.close()


# --- AC-4: влитые шаги не меняются --------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_integrated_step(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    steps = repo / "hub" / "migrations" / "steps"
    steps.mkdir(parents=True)
    (steps / "__init__.py").write_text("# loader\n")
    _write(steps, STEP_A, _A_SRC)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def test_integrated_steps_are_add_only(tmp_path, capsys):
    repo = _repo_with_integrated_step(tmp_path)
    steps = repo / "hub" / "migrations" / "steps"
    run = lambda: add_only_main(["--base", "develop", "--repo", str(repo)])  # noqa: E731

    assert run() == 0  # ничего не тронуто

    _write(steps, STEP_B, _B_SRC)  # новый файл, даже закоммиченный, - можно
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "new step")
    assert run() == 0

    _write(steps, STEP_A, _A_SRC + "# edit\n")  # правка влитого - нельзя
    assert run() == 1
    assert STEP_A in capsys.readouterr().err
    _git(repo, "commit", "-aqm", "edit")  # закоммиченная правка - тоже
    assert run() == 1
    _git(repo, "checkout", "-q", "develop", "--", str(steps / STEP_A))
    _git(repo, "commit", "-aqm", "revert")
    assert run() == 0

    (steps / STEP_A).unlink()  # удаление влитого - нельзя
    assert run() == 1
    err = capsys.readouterr().err
    assert STEP_A in err and "удалён" in err
    assert violations(repo, "develop")[0].startswith(f"hub/migrations/steps/{STEP_A}")


def test_add_only_without_base_fails_closed(tmp_path):
    repo = _repo_with_integrated_step(tmp_path)
    assert add_only_main(["--base", "no-such-ref", "--repo", str(repo)]) == 2


def test_annotated_docstring_future_and_implicit_concat_are_accepted(tmp_path):
    src = (
        '"""Шаг."""\n'
        "from __future__ import annotations\n"
        "MIGRATIONS: list[tuple[str, str]] = [\n"
        '    ("a", "SELECT "\n          "1"),\n'
        "]\n"
    )
    directory = _steps_dir(tmp_path, **{STEP_A: src})
    assert load_steps(directory, strict=True) == [("a", "SELECT 1")]


def test_step_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    src = f'open({str(marker)!r}, "w").close()\nMIGRATIONS = []\n'
    directory = _steps_dir(tmp_path, **{STEP_A: src})
    with pytest.raises(MigrationStepError):
        load_steps(directory)
    assert not marker.exists()
    # Даже корректный по форме файл не исполняется: докстрока и литерал - всё.
    _write(directory, STEP_A, f'"""x"""\nMIGRATIONS = [("n", "{marker}")]\n')
    assert load_steps(directory) == [("n", str(marker))]
    assert not marker.exists()


def test_symlink_step_is_rejected(tmp_path):
    directory = _steps_dir(tmp_path, **{STEP_A: _A_SRC})
    (directory / STEP_B).symlink_to(directory / STEP_A)
    with pytest.raises(MigrationStepError, match="не ссылкой"):
        load_steps(directory)


@pytest.mark.parametrize(
    "stray",
    [
        "README.md",
        ".keep",
        ".DS_Store",
        STEP_A + "~",
        "m٢٠٢٦١٠١٩١٢٠٠_1_x.py",
        STEP_B + "\n",
    ],
)
def test_stray_entry_does_not_stop_startup_but_reddens_the_ci_check(
    tmp_path, capsys, stray
):
    repo = _repo_with_integrated_step(tmp_path)
    steps = repo / "hub" / "migrations" / "steps"
    (steps / stray).write_text("не шаг\n")
    # Старт: шаг A на месте, посторонний файл пропущен (с предупреждением).
    assert [n for n, _ in load_steps(steps)] == ["probe_a_column", "probe_a_index"]
    assert stray_entries(steps) == [stray]
    # CI: строгая проверка краснеет и называет запись.
    assert add_only_main(["--base", "develop", "--repo", str(repo)]) == 1
    assert "посторонние записи" in capsys.readouterr().err


def test_ci_check_is_green_on_a_clean_directory(tmp_path):
    repo = _repo_with_integrated_step(tmp_path)
    assert add_only_main(["--base", "develop", "--repo", str(repo)]) == 0


def test_ci_base_is_the_state_before_the_event():
    zero = "0" * 40
    assert ci_base("pull_request", "", "develop") == "origin/develop"
    assert ci_base("pull_request", "abc", "main") == "origin/main"
    assert ci_base("push", "abc123", "") == "abc123"
    assert ci_base("push", zero, "") == "origin/develop"  # новая ветка: нет «до»
    assert ci_base("push", "", "") == "origin/develop"
    assert ci_base("workflow_dispatch", "abc", "") == "origin/develop"


def test_push_that_edits_an_integrated_step_is_red(tmp_path, capsys):
    """Push в develop: HEAD сам develop, правка уже в нём; база - before."""
    repo = _repo_with_integrated_step(tmp_path)
    steps = repo / "hub" / "migrations" / "steps"
    _git(repo, "checkout", "-q", "develop")
    before = _rev(repo, "HEAD")
    _write(steps, STEP_A, _A_SRC + "# edit after merge\n")
    _git(repo, "commit", "-aqm", "push to develop edits an integrated step")

    # Старая база (сама ветка): правка невидима - именно эта дыра закрыта.
    assert add_only_main(["--base", "develop", "--repo", str(repo)]) == 0
    capsys.readouterr()
    # База события: before. CI передаёт её флагами.
    argv = ["--github-event", "push", "--before", before, "--repo", str(repo)]
    assert add_only_main(argv) == 1
    assert STEP_A in capsys.readouterr().err
    # Новый файл тем же push - чисто.
    _git(repo, "checkout", "-q", before, "--", str(steps / STEP_A))
    _write(steps, STEP_B, _B_SRC)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "revert edit, add new step")
    assert add_only_main(argv) == 0


def test_add_only_sees_symlink_swap(tmp_path, capsys):
    repo = _repo_with_integrated_step(tmp_path)
    steps = repo / "hub" / "migrations" / "steps"
    run = lambda: add_only_main(["--base", "develop", "--repo", str(repo)])  # noqa: E731
    (steps / "link_target.txt").write_text(_A_SRC)
    (steps / STEP_A).unlink()
    (steps / STEP_A).symlink_to(steps / "link_target.txt")
    assert run() == 1
    assert "сменил тип" in capsys.readouterr().err


def test_ci_passes_the_event_base_to_the_check():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci[ci.index("- name: Migration steps add-only") :].split("- name:", 2)[1]
    for needle in ("github.event.before", "github.base_ref", "--github-event"):
        assert needle in step, needle
