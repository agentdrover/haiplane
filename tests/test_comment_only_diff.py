"""Проверка «изменены только комментарии» держится исполнением, а не доверием (#2032).

Серия чистки истории в комментариях правит сотни строк в файлах ядра. Ревью не
отличит смену текста от смены кода, а удаление ``# type: ignore`` или правка
контрактного docstring ломают инструменты и публичные поверхности. Инструмент
``scripts/comment_only_diff.py`` проверяет ОГРАНИЧЕННЫЙ класс правок (токены,
нормализованный AST, привязка директив, контрактные docstring-и); отсутствие
изменения поведения он не доказывает.

Тесты идут на временных git-репозиториях: пары ревизий — настоящие коммиты.
Коды возврата: 0 — чисто, 1 — есть нарушения, 2 — проверить не удалось.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.comment_only_diff_support import (
    CASES as _CASES,
    SCRIPTS,
    _case_params,
    _check_case,
    _git,
    _pair,
    _run,
    _run_case,
    run_process,
)

sys.path.insert(0, str(SCRIPTS))


def test_violations_and_clean_edit(tmp_path: Path) -> None:
    """AC-1: каждое нарушение — rc≠0 с перечнем, чистая правка — rc=0."""
    for index, name in enumerate(_CASES):
        workdir = tmp_path / f"c{index}"
        workdir.mkdir()
        rc, out = _run_case(workdir, name)
        _check_case(name, rc, out)


@pytest.mark.parametrize("name", _case_params())
def test_each_pair(tmp_path: Path, name: str) -> None:
    """Те же пары по одной: красный тест называет правило, которое сломалось."""
    rc, out = _run_case(tmp_path, name)
    _check_case(name, rc, out)


def test_output_names_file_line_and_kind(tmp_path: Path) -> None:
    """Перечень нарушений — файл:строка (строка базы), вид и причина."""
    repo, base, head = _pair(
        tmp_path,
        {"m.py": "a = 1\nb = 2  # noqa: E501\nc = 3\n"},
        {"m.py": "a = 1\nb = 2\nc = 4\n"},
    )
    rc, out = _run(repo, base, head, ["m.py"])
    assert rc == 1
    assert "m.py:2: [directive]" in out, out
    assert "m.py:3: [tokens]" in out, out


def test_contract_docstring_change_passes_only_when_symbol_is_allowed(
    tmp_path: Path,
) -> None:
    """Допустимое сокращение контрактного docstring названо по полному имени."""
    base = '@mcp.tool()\ndef hub_t():\n    """Длинно."""\n    return 1\n'
    head = '@mcp.tool()\ndef hub_t():\n    """Кратко."""\n    return 1\n'
    repo, b, h = _pair(tmp_path, {"m.py": base}, {"m.py": head})
    rc, out = _run(repo, b, h, ["m.py"])
    assert rc == 1 and "m.py::hub_t" in out, out
    rc, out = _run(repo, b, h, ["m.py"], "--allow-docstring", "m.py::hub_t")
    assert rc == 0, out
    rc, out = _run(repo, b, h, ["m.py"], "--allow-docstring", "m.py::other")
    assert rc == 1, out


def test_unlisted_files_and_unreadable_revision_fail(tmp_path: Path) -> None:
    """AC-2: чужой .py вне списка и нечитаемая ревизия — rc≠0 с названной причиной."""
    repo, base, head = _pair(
        tmp_path,
        {"a.py": "x = 1  # c\n", "b.py": "y = 1\n", "docs/n.md": "a\n"},
        {"a.py": "x = 1\n", "b.py": "y = 2\n", "docs/n.md": "b\n"},
    )
    rc, out = _run(repo, base, head, ["a.py"])
    assert rc == 1, out
    assert "[unlisted_file]" in out and "b.py" in out, out
    assert "docs/n.md" not in out, "не-.py вне списка нарушением не является"

    rc, out = _run(repo, "0" * 40, head, ["a.py"])
    assert rc == 2, out
    assert "revision" in out.lower() or "ревизи" in out.lower(), out

    rc, out = _run(repo, base, "no-such-ref", ["a.py"])
    assert rc == 2, out


def test_listed_files_must_be_python_and_clean_run_says_so(tmp_path: Path) -> None:
    """Неподдержанный файл — rc=2, не «чисто»; чистая правка называет свой класс."""
    repo, base, head = _pair(
        tmp_path,
        {"a.py": "x = 1  # c\n", "n.md": "a\n"},
        {"a.py": "x = 1\n", "n.md": "b\n"},
    )
    rc, out = _run(repo, base, head, ["n.md"])
    assert rc == 2, out
    rc, out = _run(repo, base, head, ["a.py"])
    assert rc == 0, out
    assert "ограниченн" in out, "результат — проверка ограниченного класса правок"


def test_unchanged_listed_file_is_clean(tmp_path: Path) -> None:
    repo, base, head = _pair(tmp_path, {"a.py": "x = 1\n"}, {"b.txt": "q\n"})
    rc, out = _run(repo, base, head, ["a.py"])
    assert rc == 0, out


def test_in_process_main_returns_the_same_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import comment_only_diff

    repo, base, head = _pair(tmp_path, {"a.py": "x = 1  # c\n"}, {"a.py": "x = 2\n"})
    rc = comment_only_diff.main(
        ["--repo", str(repo), "--base", base, "--head", head, "a.py"]
    )
    assert rc == 1
    assert "[tokens]" in capsys.readouterr().out


def test_ast_check_sees_a_type_comment_edit() -> None:
    """Нормализованный AST (type_comments=True) различает правку типового комментария.

    Через CLI её первой ловит проверка директив, поэтому сам AST-шаг проверяется
    напрямую: защита в глубину не должна тихо перестать работать.
    """
    import comment_only_diff as cod

    def parsed(text: str) -> cod.Parsed:
        return cod.parse_source(text.encode(), "m.py")

    same = cod.compare_ast(
        "m.py",
        parsed("x = []  # type: list[int]\n"),
        parsed("x = []  # type: list[int]\n"),
    )
    assert same == []
    changed = cod.compare_ast(
        "m.py",
        parsed("x = []  # type: list[int]\n"),
        parsed("x = []  # type: list[str]\n"),
    )
    assert [v.kind for v in changed] == ["ast"]


def test_directive_noise_is_not_reported_after_code_diverges(tmp_path: Path) -> None:
    """После расхождения кода индексы-якоря сдвинуты: шум о директивах не выводится."""
    repo, base, head = _pair(
        tmp_path,
        {"m.py": "a = 1\nb = 2  # noqa: E501\n"},
        {"m.py": "a = 1; z = 0\nb = 2  # noqa: E501\n"},
    )
    rc, out = _run(repo, base, head, ["m.py"])
    assert rc == 1 and "[tokens]" in out, out
    assert "[directive]" not in out, out


def test_unchanged_listed_file_must_still_be_checkable(tmp_path: Path) -> None:
    """Файл списка без правок, но с ошибкой разбора, — «не проверено», не «чисто»."""
    repo, base, head = _pair(
        tmp_path,
        {"bad.py": "x = (\n", "odd.py": "# \xff\nx = 1\n", "ok.py": "y = 1  # c\n"},
        {"ok.py": "y = 1\n"},
    )
    rc, out = _run(repo, base, head, ["ok.py", "bad.py"])
    assert rc == 2 and "bad.py" in out, out


def test_non_utf8_files_are_not_checked_as_utf8(tmp_path: Path) -> None:
    """Cookie другой кодировки меняет значения строк: такой файл проверить нельзя."""
    latin = "# coding: latin-1\n'doc'\nx = 'é'\n"
    repo, base, head = _pair(
        tmp_path, {"m.py": latin}, {"m.py": "# coding: latin-1\nx = 'é'\n"}
    )
    rc, out = _run(repo, base, head, ["m.py"])
    assert rc == 2 and "кодировка" in out, out


def test_path_with_a_newline_is_not_read_as_another_file(tmp_path: Path) -> None:
    """Построчный протокол git не должен подменять файл: такой путь отвергается."""
    repo, base, head = _pair(tmp_path, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    rc, out = _run(repo, base, head, ["a.py\nmissing.py"])
    assert rc == 2 and "управляющ" in out, out


def test_mode_change_and_symlink_are_not_comment_edits(tmp_path: Path) -> None:
    """Исполняемый бит и превращение файла в ссылку — не правка комментариев."""
    repo, base, _ = _pair(
        tmp_path, {"a.py": "print(1)\n", "payload.py": "print(42)\n"}, {"b.txt": "q\n"}
    )
    _git(repo, "update-index", "--chmod=+x", "a.py")
    _git(repo, "commit", "-q", "-m", "exec bit")
    mode_head = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, base, mode_head, ["a.py"])
    assert rc == 1 and "[file_mode]" in out, out

    (repo / "a.py").unlink()
    (repo / "a.py").symlink_to("payload.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "symlink")
    link_head = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, base, link_head, ["a.py"])
    assert rc == 2 and "ссылк" in out, out


def test_real_process_exit_codes(tmp_path: Path) -> None:
    """Код возврата процесса: 0 чисто, 1 нарушение, 2 не удалось проверить."""
    repo, base, head = _pair(tmp_path, {"a.py": "x = 1  # c\n"}, {"a.py": "x = 1\n"})
    assert run_process(repo, base, head, ["a.py"])[0] == 0
    repo2 = tmp_path / "second"
    repo2.mkdir()
    repo2, base2, head2 = _pair(repo2, {"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    assert run_process(repo2, base2, head2, ["a.py"])[0] == 1
    assert run_process(repo2, "0" * 40, head2, ["a.py"])[0] == 2


def test_symlinked_python_file_makes_the_index_incomplete(tmp_path: Path) -> None:
    """Ссылка вместо .py в репозитории: индекс контрактов неполон, «чисто» нельзя."""
    repo, base, _ = _pair(
        tmp_path, {"m.py": "x = 1  # c\n", "real.txt": "q\n"}, {"n.txt": "z\n"}
    )
    (repo / "m.py").write_text("x = 1\n")
    (repo / "link.py").symlink_to("real.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "link")
    head = _git(repo, "rev-parse", "HEAD")
    rc, out = _run(repo, base, head, ["m.py"])
    assert rc == 2 and "неполон" in out, out
