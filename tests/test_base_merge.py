"""Авторская правка по смыслу, а не байт в байт (#1361).

Одна функция — ``base_merge.author_edit_same`` — отвечает на вопрос «та же ли
авторская правка» и гейту доставки (#1233), и заказу ревью на пересдаче. Здесь
она проверяется на НАСТОЯЩЕМ git: база тронула файл автора, конфликт разрешён
тремя способами, и только один из них — «оставить обе стороны, ничего не
дописав» — сохраняет правку той же.

Измеренный случай 23.09.2026: #1242, #1254, #1333, #1334, #1337 — каждое
слияние develop в одобренную ветку с конфликтом в общем файле снимало
одобрение или покупало новое полное ревью, хотя отсортированные +/- строки
автора совпадали. Сортировка же — множество, и множество не видит
перестановки: в #1337 миграция переехала в конец списка, а для миграций
порядок — это смысл. Поэтому сравнение упорядоченное.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hub.integrations.git_ops import GitOpsIntegration
from hub.services import base_merge

BRANCH = "task-1361/probe"

_COMMON = (
    "HEADER = 1\ndef a():\n    return 1\ndef b():\n    return 2\nTAIL = 'common'\n"
)

# База трогает ТОТ ЖЕ файл трижды: правит строку, вставляет строку сверху
# (сдвигает смещения ханков автора) и дописывает хвост (конфликт с автором).
_BASE = (
    "HEADER = 2\n"
    "IMPORTED = True\n"
    "def a():\n"
    "    return 1\n"
    "def b():\n"
    "    return 2\n"
    "TAIL = 'common'\n"
    "BASE_TAIL = 'base'\n"
)

_BRANCH = (
    "HEADER = 1\n"
    "def a():\n"
    "    return 1\n"
    "def b():\n"
    "    return 20\n"
    "TAIL = 'common'\n"
    "AUTHOR_FIRST = 'first'\n"
    "AUTHOR_SECOND = 'second'\n"
)

_MERGED_HEAD = (
    "HEADER = 2\n"
    "IMPORTED = True\n"
    "def a():\n"
    "    return 1\n"
    "def b():\n"
    "    return 20\n"
    "TAIL = 'common'\n"
)

RESOLUTIONS = {
    # Обе стороны, авторская первой, ни строки сверх того.
    "both": _MERGED_HEAD
    + "AUTHOR_FIRST = 'first'\nAUTHOR_SECOND = 'second'\nBASE_TAIL = 'base'\n",
    # Те же строки автора, но в другом порядке — это изменение (#1337).
    "reordered": _MERGED_HEAD
    + "AUTHOR_SECOND = 'second'\nAUTHOR_FIRST = 'first'\nBASE_TAIL = 'base'\n",
    # Строка сопряжения, которой ревью не видело, — новая авторская правка.
    "new_line": _MERGED_HEAD + "AUTHOR_FIRST = 'first'\nAUTHOR_SECOND = 'second'\n"
    "GLUE = 'resolution'\nBASE_TAIL = 'base'\n",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _merge_repo(
    tmp_path: Path, name: str, common: str, branch: str, base: str, resolved: str
) -> tuple[Path, str, str]:
    """Клон, где база и ветка правят один файл, а конфликт разрешён руками.

    Возвращает ``(клон, одобренная вершина, вершина после слияния)``.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    target = work / name
    target.write_text(common)
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "common", cwd=work)
    _git("push", "-q", "origin", "develop", cwd=work)

    _git("checkout", "-q", "-b", BRANCH, cwd=work)
    target.write_text(branch)
    _git("commit", "-q", "-am", "author", cwd=work)
    _git("push", "-q", "origin", BRANCH, cwd=work)
    approved = _git("rev-parse", "HEAD", cwd=work)

    _git("checkout", "-q", "develop", cwd=work)
    target.write_text(base)
    _git("commit", "-q", "-am", "base", cwd=work)
    _git("push", "-q", "origin", "develop", cwd=work)

    _git("checkout", "-q", BRANCH, cwd=work)
    merged = subprocess.run(
        ["git", "merge", "-q", "--no-edit", "origin/develop"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert merged.returncode != 0, "предусловие: слияние обязано конфликтовать"
    target.write_text(resolved)
    _git("add", name, cwd=work)
    _git("commit", "-q", "--no-edit", cwd=work)
    _git("push", "-q", "origin", BRANCH, cwd=work)
    return work, approved, _git("rev-parse", "HEAD", cwd=work)


def shared_file_merge(tmp_path: Path, resolution: str) -> tuple[Path, str, str]:
    """База тронула ``mod.py`` автора; разрешение — одно из ``RESOLUTIONS``."""
    return _merge_repo(
        tmp_path, "mod.py", _COMMON, _BRANCH, _BASE, RESOLUTIONS[resolution]
    )


async def _diffs(tmp_path: Path, resolution: str) -> tuple[str, str]:
    return await _real_diffs(*shared_file_merge(tmp_path, resolution))


async def _real_diffs(work: Path, approved: str, tip: str) -> tuple[str, str]:
    ops = GitOpsIntegration()
    before = await ops.branch_diff(str(work), "develop", approved)
    after = await ops.branch_diff(str(work), "develop", tip)
    assert before is not None and after is not None
    return before, after


# ---- AC-1 на уровне правила: общий файл, обе стороны, ни строки сверх ----


async def test_keeping_both_sides_of_a_shared_file_is_the_same_edit(tmp_path):
    before, after = await _diffs(tmp_path, "both")
    assert before != after, (
        "предусловие: побайтно дифф РАЗНЫЙ — строка index и смещения ханков "
        "сменились, иначе тест проверял бы старое правило #1233"
    )
    same, why = base_merge.author_edit_same(before, after)
    assert same, why
    assert "та же" in why, "сохранение без названной причины — это доверие"


# ---- AC-2: перестановка и строка разрешения — это изменение ----


async def test_reordered_or_new_author_lines_are_a_change(tmp_path):
    for case in ("reordered", "new_line"):
        before, after = await _diffs(tmp_path / case, case)
        same, why = base_merge.author_edit_same(before, after)
        assert not same, f"{case}: {why}"
        assert "mod.py" in why, f"{case}: отличие обязано быть названо файлом: {why}"


# ---- Находка ревью: строка содержимого, похожая на заголовок файла ----
#
# В ``-U0`` удалённая строка ``-- verbose`` печатается как ``--- verbose``, а
# добавленная ``++ x`` — как ``+++ x``. Первая редакция отбрасывала ``---``/
# ``+++`` в любом месте диффа, и такие правки пропадали с обеих сторон:
# разрешение, выронившее строку базы ``-- base only``, читалось «той же
# правкой». Заголовок файла — только до первого ``@@`` этого файла.

_SQL_COMMON = "SELECT 1;\n-- verbose\n-- keep\nTAIL;\n"
_SQL_BRANCH = "SELECT 1;\n-- keep\nTAIL;\n++ x\n"
_SQL_BASE = "SELECT 2;\n-- verbose\n-- keep\nTAIL;\n-- base only\n"
# Разрешение взяло правку базы сверху, но выронило её хвост «-- base only».
_SQL_DROPS_BASE_LINE = "SELECT 2;\n-- keep\nTAIL;\n++ x\n"


async def test_a_dropped_line_that_looks_like_a_file_header_is_a_change(tmp_path):
    before, after = await _real_diffs(
        *_merge_repo(
            tmp_path,
            "schema.sql",
            _SQL_COMMON,
            _SQL_BRANCH,
            _SQL_BASE,
            _SQL_DROPS_BASE_LINE,
        )
    )
    assert "\n--- verbose\n" in before and "\n+++ x" in before, (
        "предусловие: в -U0 правки автора выглядят как заголовки файла"
    )
    assert "\n--- base only" in after, "предусловие: разрешение выронило строку"
    same, why = base_merge.author_edit_same(before, after)
    assert not same, why
    assert "schema.sql" in why


def test_content_lines_after_a_hunk_header_are_the_author_edit():
    before = (
        "diff --git a/s.sql b/s.sql\n"
        "--- a/s.sql\n"
        "+++ b/s.sql\n"
        "@@ -2 +1,0 @@\n"
        "--- verbose\n"
    )
    assert not base_merge.author_edit_same(before, before.replace("verbose", "quiet"))[
        0
    ]
    no_content = before.rsplit("--- verbose\n", 1)[0]
    assert not base_merge.author_edit_same(before, no_content)[0], (
        "строка «--- …» после @@ — содержимое, а не заголовок"
    )


# ---- Разбор диффа: что отбрасывается, а что — нет ----

_BEFORE = (
    "diff --git a/m.py b/m.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/m.py\n"
    "+++ b/m.py\n"
    "@@ -5 +5 @@ def b():\n"
    "-    return 2\n"
    "+    return 20\n"
    "@@ -6,0 +7,2 @@ TAIL = 'common'\n"
    "+X = 1\n"
    "+Y = 2\n"
)


def test_headers_index_and_hunk_offsets_are_not_the_author_edit():
    after = (
        _BEFORE.replace("index 1111111..2222222", "index 3333333..4444444")
        .replace("@@ -5 +5 @@ def b():", "@@ -6 +6 @@ def b():")
        .replace("@@ -6,0 +7,2 @@", "@@ -7,0 +8,2 @@")
    )
    assert base_merge.author_edit_same(_BEFORE, after)[0]


def test_an_edited_author_line_is_a_change():
    # Мутационный напарник «нормализация съедает правку»: сравнивать только
    # знаки +/- без содержимого строк — и этот тест обязан упасть.
    after = _BEFORE.replace("+Y = 2\n", "+Y = 3\n")
    same, why = base_merge.author_edit_same(_BEFORE, after)
    assert not same and "m.py" in why


def test_a_dropped_author_line_is_a_change():
    after = _BEFORE.replace("+Y = 2\n", "")
    assert not base_merge.author_edit_same(_BEFORE, after)[0]


def test_a_removed_line_the_author_did_not_remove_is_a_change():
    after = _BEFORE.replace("-    return 2\n", "-    return 2\n-    pass\n")
    assert not base_merge.author_edit_same(_BEFORE, after)[0]


def test_a_renamed_file_is_a_change():
    after = _BEFORE.replace("diff --git a/m.py b/m.py", "diff --git a/m.py b/n.py")
    same, why = base_merge.author_edit_same(_BEFORE, after)
    assert not same and "n.py" in why


def test_a_file_the_author_did_not_touch_is_a_change():
    extra = "diff --git a/z.py b/z.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert not base_merge.author_edit_same(_BEFORE, _BEFORE + extra)[0]


def test_a_binary_file_keeps_its_index_line():
    # У бинарного файла нет строк +/-: содержимое видно только по index.
    # Отбросить его — и любая подмена картинки прошла бы «той же правкой».
    one = (
        "diff --git a/p.png b/p.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/p.png and b/p.png differ\n"
    )
    two = one.replace("2222222", "3333333")
    assert not base_merge.author_edit_same(one, two)[0]
    assert base_merge.author_edit_same(one, one)[0]


def test_an_unreadable_diff_is_never_the_same_edit():
    assert not base_merge.author_edit_same(None, _BEFORE)[0]
    assert not base_merge.author_edit_same(_BEFORE, None)[0]
