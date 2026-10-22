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

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "comment_only_diff.py"
sys.path.insert(0, str(SCRIPTS))

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, files: Mapping[str, str | None], msg: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _pair(
    tmp_path: Path,
    base: Mapping[str, str],
    head: Mapping[str, str | None],
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base_sha = _commit(repo, base, "base")
    head_sha = _commit(repo, head, "head")
    return repo, base_sha, head_sha


def _run(
    repo: Path,
    base: str,
    head: str,
    files: list[str],
    *extra: str,
) -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            *extra,
            *files,
        ],
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    )
    return proc.returncode, proc.stdout + proc.stderr


# --- таблица пар «было → стало» -------------------------------------------------
#
# (base, head, kind): kind=None — чистая правка (rc 0); строка — ожидаемый
# вид нарушения в выводе (rc 1); "error" — проверить не удалось (rc 2).
# Один файл m.py; дополнительные файлы кладёт ключ "extra".

CLEAN = None

_CASES: dict[str, dict] = {
    "clean_comment_edit_cyrillic": {
        "base": (
            "x = 1  # старый комментарий\n"
            "# история #12\n"
            "def f():\n"
            '    """Док"""\n'
            '    return "кириллица"\n'
        ),
        "head": ('x = 1  # новый\n# #12 причина\ndef f():\n    return "кириллица"\n'),
        "kind": CLEAN,
    },
    "clean_byte_offsets_after_cyrillic_name": {
        "base": 'def ж(): "док"; return 2\n',
        "head": "def ж(): return 2\n",
        "kind": CLEAN,
    },
    "clean_bracketed_docstring": {
        "base": 'def f():\n    ("""doc\n    more""")\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": CLEAN,
    },
    "clean_concatenated_docstring": {
        "base": 'def f():\n    ("a"\n     "b")\n    return 1\n',
        "head": 'def f():\n    """x"""\n    return 1\n',
        "kind": CLEAN,
    },
    "clean_module_and_class_docstrings": {
        "base": '"""old"""\nclass A:\n    """old"""\n    y = 1\n',
        "head": "class A:\n    y = 1\n",
        "kind": CLEAN,
    },
    "clean_directive_keeps_anchor_prose_changes": {
        "base": "x = 1  # история  # noqa: BLE001\n",
        "head": "x = 1  # noqa: BLE001\n",
        "kind": CLEAN,
    },
    "clean_directive_survives_deleted_comment_lines": {
        "base": "# a\n# b\ncall(\n    1,  # c\n    2,  # type: ignore[arg-type]\n)\n",
        "head": "call(\n    1,\n    2,  # type: ignore[arg-type]\n)\n",
        "kind": CLEAN,
    },
    "clean_ref_kept_in_file": {
        "base": "# причина #1234 и #99\nx = 1\n",
        "head": "# #1234, #99\nx = 1\n",
        "kind": CLEAN,
    },
    "clean_ref_moved_to_invariants": {
        "base": "# причина #1234\nx = 1\n",
        "head": "x = 1\n",
        "extra_head": {"docs/agent-context/invariants.md": "см. #1234\n"},
        "kind": CLEAN,
    },
    "code_changed": {
        "base": "def f():\n    return 1\n",
        "head": "def f():\n    return 2\n",
        "kind": "tokens",
    },
    "plain_string_changed": {
        "base": 'x = "a"\n',
        "head": 'x = "b"\n',
        "kind": "tokens",
    },
    "fstring_changed": {
        "base": 'x = f"a{y}"\n',
        "head": 'x = f"b{y}"\n',
        "kind": "tokens",
    },
    "fstring_is_not_a_docstring": {
        "base": 'def f():\n    f"doc"\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": "tokens",
    },
    "bytes_is_not_a_docstring": {
        "base": 'def f():\n    b"doc"\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": "tokens",
    },
    "second_string_is_not_a_docstring": {
        "base": 'def f():\n    x = 1\n    "later"\n    return x\n',
        "head": "def f():\n    x = 1\n    return x\n",
        "kind": "tokens",
    },
    "empty_body_after_docstring_removal": {
        "base": 'def f():\n    """doc"""\n',
        "head": "def f():\n    pass\n",
        "kind": "tokens",
    },
    "noqa_removed": {
        "base": "x = 1  # noqa: BLE001\n",
        "head": "x = 1\n",
        "kind": "directive",
    },
    "noqa_codes_changed": {
        "base": "x = 1  # noqa: BLE001\n",
        "head": "x = 1  # noqa: E501\n",
        "kind": "directive",
    },
    "noqa_added": {
        "base": "x = 1\n",
        "head": "x = 1  # noqa\n",
        "kind": "directive",
    },
    "noqa_moved_to_neighbour_statement": {
        "base": "x = 1  # noqa: E501\ny = 2\n",
        "head": "x = 1\ny = 2  # noqa: E501\n",
        "kind": "directive",
    },
    "noqa_moved_to_other_line_of_one_statement": {
        "base": "call(\n    1,  # noqa: E501\n    2,\n)\n",
        "head": "call(\n    1,\n    2,  # noqa: E501\n)\n",
        "kind": "directive",
    },
    "type_ignore_removed": {
        "base": "x: int = f()  # type: ignore[arg-type]\n",
        "head": "x: int = f()\n",
        "kind": "directive",
    },
    "type_comment_removed": {
        "base": "x = []  # type: list[int]\n",
        "head": "x = []\n",
        "kind": "directive",
    },
    "pragma_removed": {
        "base": "def f():  # pragma: no cover\n    return 1\n",
        "head": "def f():\n    return 1\n",
        "kind": "directive",
    },
    "allowlist_secret_removed": {
        "base": 'k = "abc"  # pragma: allowlist secret\n',
        "head": 'k = "abc"\n',
        "kind": "directive",
    },
    "fmt_skip_removed": {
        "base": "x   =   1  # fmt: skip\n",
        "head": "x   =   1\n",
        "kind": "directive",
    },
    "nosec_removed": {
        "base": 'q = "select"  # nosec B608\n',
        "head": 'q = "select"\n',
        "kind": "directive",
    },
    "shebang_removed": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": "x = 1\n",
        "kind": "directive",
    },
    "coding_cookie_removed": {
        "base": "# -*- coding: utf-8 -*-\nx = 1\n",
        "head": "x = 1\n",
        "kind": "directive",
    },
    "syntax_error_in_head": {
        "base": "x = 1\n",
        "head": "x = (\n",
        "kind": "error",
    },
    "syntax_error_in_base": {
        "base": "x = (\n",
        "head": "x = 1\n",
        "kind": "error",
    },
    "contract_mcp_tool_docstring": {
        "base": (
            "@mcp.tool()\n"
            "def hub_t():\n"
            '    """Описание в tools/list."""\n'
            "    return 1\n"
        ),
        "head": ('@mcp.tool()\ndef hub_t():\n    """Короче."""\n    return 1\n'),
        "kind": "contract_docstring",
    },
    "contract_mcp_tool_docstring_deleted": {
        "base": '@mcp.tool()\ndef hub_t():\n    """Описание."""\n    return 1\n',
        "head": "@mcp.tool()\ndef hub_t():\n    return 1\n",
        "kind": "contract_docstring",
    },
    "contract_fastapi_route_docstring": {
        "base": '@app.get("/x")\nasync def h():\n    """OpenAPI."""\n    return 1\n',
        "head": '@app.get("/x")\nasync def h():\n    """Другое."""\n    return 1\n',
        "kind": "contract_docstring",
    },
    "fastapi_route_with_explicit_description_is_free": {
        "base": (
            '@app.get("/x", description="d")\nasync def h():\n    """A."""\n'
            "    return 1\n"
        ),
        "head": (
            '@app.get("/x", description="d")\nasync def h():\n    """B."""\n'
            "    return 1\n"
        ),
        "kind": CLEAN,
    },
    "fastapi_route_out_of_schema_is_free": {
        "base": (
            '@app.get("/x", include_in_schema=False)\nasync def h():\n'
            '    """A."""\n    return 1\n'
        ),
        "head": (
            '@app.get("/x", include_in_schema=False)\nasync def h():\n'
            '    """B."""\n    return 1\n'
        ),
        "kind": CLEAN,
    },
    "plain_function_docstring_is_free": {
        "base": 'def g():\n    """A."""\n    return 1\n',
        "head": 'def g():\n    """B."""\n    return 1\n',
        "kind": CLEAN,
    },
    "contract_pydantic_model_docstring": {
        "base": 'class M(BaseModel):\n    """Схема."""\n    x: int\n',
        "head": 'class M(BaseModel):\n    """Иначе."""\n    x: int\n',
        "kind": "contract_docstring",
    },
    "contract_enum_docstring": {
        "base": 'class E(str, Enum):\n    """Вердикт."""\n    A = "a"\n',
        "head": 'class E(str, Enum):\n    A = "a"\n',
        "kind": "contract_docstring",
    },
    "contract_pydantic_model_inherited_in_file": {
        "base": (
            "class M(BaseModel):\n    x: int\n"
            'class N(M):\n    """Схема."""\n    y: int\n'
        ),
        "head": "class M(BaseModel):\n    x: int\nclass N(M):\n    y: int\n",
        "kind": "contract_docstring",
    },
    "contract_pydantic_model_inherited_across_files": {
        "base": 'from models import M\nclass N(M):\n    """Схема."""\n    y: int\n',
        "head": "from models import M\nclass N(M):\n    y: int\n",
        "extra_both": {"models.py": "class M(BaseModel):\n    x: int\n"},
        "kind": "contract_docstring",
    },
    "contract_argparse_module_docstring": {
        "base": (
            '"""Справка скрипта."""\n'
            "import argparse\n"
            "p = argparse.ArgumentParser(description=__doc__)\n"
        ),
        "head": (
            '"""Другая справка."""\n'
            "import argparse\n"
            "p = argparse.ArgumentParser(description=__doc__)\n"
        ),
        "kind": "contract_docstring",
    },
    "contract_docstring_read_by_a_test": {
        "base": 'def record():\n    """Нужные слова."""\n    return 1\n',
        "head": 'def record():\n    """Иначе."""\n    return 1\n',
        "extra_both": {
            "tests/test_x.py": (
                "import inspect\nimport m\n\n"
                "def test_doc():\n"
                "    assert 'слова' in inspect.getdoc(m.record)\n"
            )
        },
        "kind": "contract_docstring",
    },
    "contract_docstring_read_by_dunder_doc_in_a_test": {
        "base": (
            'class G:\n    def branch(self):\n        """Исходы."""\n        return 1\n'
        ),
        "head": "class G:\n    def branch(self):\n        return 1\n",
        "extra_both": {
            "tests/test_y.py": (
                "from m import G\n\n"
                "def test_doc():\n"
                "    assert 'Исходы' in G.branch.__doc__\n"
            )
        },
        "kind": "contract_docstring",
    },
    "contract_registered_by_add_tool": {
        "base": (
            'def hub_t():\n    """Описание."""\n    return 1\nmcp.add_tool(hub_t)\n'
        ),
        "head": "def hub_t():\n    return 1\nmcp.add_tool(hub_t)\n",
        "kind": "contract_docstring",
    },
    "contract_cli_command_help": {
        "base": '@app.command()\ndef run():\n    """Справка."""\n    return 1\n',
        "head": '@app.command()\ndef run():\n    """Иначе."""\n    return 1\n',
        "kind": "contract_docstring",
    },
    "coding_cookie_moved_after_code": {
        "base": '# coding: utf-8\n"""d"""\nx = 1\n',
        "head": '"""d"""\n# coding: utf-8\nx = 1\n',
        "kind": "directive",
    },
    "nosec_test_ids_changed": {
        "base": "x = 1  # nosec: B101\n",
        "head": "x = 1  # nosec: B102\n",
        "kind": "directive",
    },
    "nosec_test_names_changed": {
        "base": "x = 1  # nosec assert_used\n",
        "head": "x = 1  # nosec exec_used\n",
        "kind": "directive",
    },
    "type_ignore_case_changed": {
        "base": "x = f()  # type: ignore[name-defined]\n",
        "head": "x = f()  # type: ignore[NAME-DEFINED]\n",
        "kind": "directive",
    },
    "fmt_directive_case_changed": {
        "base": "x   = 1  # fmt: skip\n",
        "head": "x   = 1  # fmt: SKIP\n",
        "kind": "directive",
    },
    "directive_order_swapped": {
        "base": "# fmt: off\n# fmt: on\nx = 1\n",
        "head": "# fmt: on\n# fmt: off\nx = 1\n",
        "kind": "directive",
    },
    "contract_route_with_computed_path": {
        "base": 'P = "/x"\n@app.get(P)\nasync def h():\n    "old"\n    return 1\n',
        "head": 'P = "/x"\n@app.get(P)\nasync def h():\n    "new"\n    return 1\n',
        "kind": "contract_docstring",
    },
    "contract_route_with_description_none": {
        "base": '@app.get("/x", description=None)\ndef h():\n    "old"\n    return 1\n',
        "head": '@app.get("/x", description=None)\ndef h():\n    "new"\n    return 1\n',
        "kind": "contract_docstring",
    },
    "contract_route_with_empty_description": {
        "base": '@app.get("/x", description="")\ndef h():\n    "old"\n    return 1\n',
        "head": '@app.get("/x", description="")\ndef h():\n    "new"\n    return 1\n',
        "kind": "contract_docstring",
    },
    "contract_pydantic_alias_import": {
        "base": "from pydantic import BaseModel as BM\nclass M(BM):\n    'old'\n    x: int\n",
        "head": "from pydantic import BaseModel as BM\nclass M(BM):\n    'new'\n    x: int\n",
        "kind": "contract_docstring",
    },
    "contract_parent_imported_under_alias": {
        "base": "from models import M as Parent\nclass N(Parent):\n    'old'\n    y: int\n",
        "head": "from models import M as Parent\nclass N(Parent):\n    'new'\n    y: int\n",
        "extra_both": {"models.py": "class M(BaseModel):\n    x: int\n"},
        "kind": "contract_docstring",
    },
    "same_class_name_elsewhere_is_not_a_false_red": {
        "base": "class Parent: pass\nclass A(Parent):\n    'old'\n    x: int\n",
        "head": "class Parent: pass\nclass A(Parent):\n    'new'\n    x: int\n",
        "extra_both": {
            "other.py": "from pydantic import BaseModel\nclass Parent(BaseModel):\n    y: int\n"
        },
        "kind": CLEAN,
    },
    "contract_module_docstring_read_by_test": {
        "base": '"old"\nx = 1\n',
        "head": '"new"\nx = 1\n',
        "extra_both": {
            "tests/test_m.py": "import inspect\nimport m\nassert inspect.getdoc(m) == 'old'\n"
        },
        "kind": "contract_docstring",
    },
    "contract_function_read_through_an_import_alias": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "tests/test_m.py": "from m import f as g\nassert g.__doc__ == 'old'\n"
        },
        "kind": "contract_docstring",
    },
    "contract_registered_by_keyword": {
        "base": 'def f():\n    "old"\n    return 1\napp.add_api_route("/x", endpoint=f)\n',
        "head": 'def f():\n    "new"\n    return 1\napp.add_api_route("/x", endpoint=f)\n',
        "kind": "contract_docstring",
    },
    "contract_registered_in_another_file": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {"app.py": "from m import f\nmcp.add_tool(f)\n"},
        "kind": "contract_docstring",
    },
    "identical_helper_does_not_hide_the_test": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "helper.py": "import inspect\nfrom m import f\nassert inspect.getdoc(f) == 'old'\n",
            "tests/test_m.py": "import inspect\nfrom m import f\nassert inspect.getdoc(f) == 'old'\n",
        },
        "kind": "contract_docstring",
    },
    "string_in_a_nested_block_is_not_the_docstring": {
        "base": 'def f():\n    "doc"\n    if True:\n        "code"\n    return 1\n',
        "head": 'def f():\n    if True:\n        "code"\n    return 1\n',
        "kind": CLEAN,
    },
    "lost_issue_reference": {
        "base": "# причина #1234\nx = 1\n",
        "head": "# причина\nx = 1\n",
        "kind": "ref_lost",
    },
    "listed_file_missing_in_head": {
        "base": "x = 1\n",
        "head": None,
        "kind": "file_changed",
    },
}


def _case_params() -> list:
    return [pytest.param(name, id=name) for name in _CASES]


def _run_case(tmp_path: Path, name: str) -> tuple[int, str]:
    case = _CASES[name]
    both = case.get("extra_both", {})
    base = {"m.py": case["base"], **both}
    head: dict[str, str | None] = {"m.py": case["head"], **both}
    head.update(case.get("extra_head", {}))
    repo, base_sha, head_sha = _pair(tmp_path, base, head)
    return _run(repo, base_sha, head_sha, ["m.py"])


def _check_case(name: str, rc: int, out: str) -> None:
    kind = _CASES[name]["kind"]
    if kind is CLEAN:
        assert rc == 0, f"{name}: ждали rc=0, получили {rc}\n{out}"
    elif kind == "error":
        assert rc == 2, f"{name}: ждали rc=2, получили {rc}\n{out}"
    else:
        assert rc == 1, f"{name}: ждали rc=1, получили {rc}\n{out}"
        assert f"[{kind}]" in out, f"{name}: нет нарушения {kind}\n{out}"


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
