"""Таблица пар «было → стало» для tests/test_comment_only_diff.py (#2032).

Запись — пара одного файла (плюс неизменные соседи ``extra_both``, правка
соседей ``extra_head``) и ожидаемый вид нарушения: ``CLEAN`` — rc 0, имя вида —
rc 1 с ``[вид]`` в выводе, ``"error"`` — rc 2. Ключ ``file`` задаёт файл списка
(по умолчанию ``m.py``). Таблица вынесена, чтобы каждый файл тестов оставался
в пороге размера дерева tests (800 строк).
"""

from __future__ import annotations

# --- таблица пар «было → стало» -------------------------------------------------
#
# (base, head, kind): kind=None — чистая правка (rc 0); строка — ожидаемый
# вид нарушения в выводе (rc 1); "error" — проверить не удалось (rc 2).
# Один файл m.py; дополнительные файлы кладёт ключ "extra".

CLEAN = None

CASES: dict[str, dict] = {
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
    "fmt_off_indent_changed": {
        "base": "def f():\n    x   = 1\n    # fmt: off\ny   = 2\n",
        "head": "def f():\n    x   = 1\n# fmt: off\ny   = 2\n",
        "kind": "directive",
    },
    "shebang_indented": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": " #!/usr/bin/env python3\nx = 1\n",
        "kind": "directive",
    },
    "line_endings_changed_in_a_script": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": "#!/usr/bin/env python3\r\nx = 1\r\n",
        "kind": "tokens",
    },
    "noqa_code_got_a_suffix": {
        "base": "x = foo  # noqa: F821\n",
        "head": "x = foo  # noqa: F821XYZ\n",
        "kind": "directive",
    },
    "contract_local_import_does_not_shadow_the_global_one": {
        "base": (
            "from pydantic import BaseModel as Parent\n"
            "class M(Parent):\n    'old'\n    x: int\n"
            "def helper():\n    from ordinary import Parent\n"
        ),
        "head": (
            "from pydantic import BaseModel as Parent\n"
            "class M(Parent):\n    'new'\n    x: int\n"
            "def helper():\n    from ordinary import Parent\n"
        ),
        "extra_both": {"ordinary.py": "class Parent:\n    pass\n"},
        "kind": "contract_docstring",
    },
    "contract_class_reexported_under_an_alias": {
        "base": "from models import Parent\nclass M(Parent):\n    'old'\n    x: int\n",
        "head": "from models import Parent\nclass M(Parent):\n    'new'\n    x: int\n",
        "extra_both": {"models.py": "from pydantic import BaseModel as Parent\n"},
        "kind": "contract_docstring",
    },
    "contract_parent_declared_with_a_tab": {
        "base": "from models import Parent\nclass M(Parent):\n    'old'\n    x: int\n",
        "head": "from models import Parent\nclass M(Parent):\n    'new'\n    x: int\n",
        "extra_both": {
            "models.py": "from pydantic import BaseModel\nclass\tParent(BaseModel):\n    pass\n"
        },
        "kind": "contract_docstring",
    },
    "contract_parent_in_a_latin1_file": {
        "base": "from models import Parent\nclass M(Parent):\n    'old'\n    x: int\n",
        "head": "from models import Parent\nclass M(Parent):\n    'new'\n    x: int\n",
        "extra_both": {
            "models.py": (
                b"# coding: latin-1\nfrom pydantic import BaseModel\n"
                b"class Parent(BaseModel): pass\n\xe9=1\n"
            )
        },
        "kind": "contract_docstring",
    },
    "contract_getdoc_with_a_keyword_argument": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "tests/test_m.py": "import inspect\nimport m\nassert inspect.getdoc(object=m.f)\n"
        },
        "kind": "contract_docstring",
    },
    "contract_package_docstring_read_by_a_test": {
        "file": "pkg/__init__.py",
        "base": '"old"\nx = 1\n',
        "head": '"new"\nx = 1\n',
        "extra_both": {
            "tests/test_pkg.py": "import pkg\nassert pkg.__doc__ == 'old'\n"
        },
        "kind": "contract_docstring",
    },
    "contract_decorator_imported_under_an_alias": {
        "base": "from click import command as cmd\n@cmd()\ndef f():\n    'old'\n    return 1\n",
        "head": "from click import command as cmd\n@cmd()\ndef f():\n    'new'\n    return 1\n",
        "kind": "contract_docstring",
    },
    "another_module_symbol_read_by_a_test_is_not_a_false_red": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "other.py": 'def f():\n    "external"\n    return 2\n',
            "tests/test_other.py": "import other\nassert other.f.__doc__ == 'external'\n",
        },
        "kind": CLEAN,
    },
    "reexport_to_a_plain_class_is_not_a_false_red": {
        "base": "from ordinary import Parent\nclass M(Parent):\n    'old'\n    pass\n",
        "head": "from ordinary import Parent\nclass M(Parent):\n    'new'\n    pass\n",
        "extra_both": {
            "ordinary/__init__.py": "from ordinary.impl import Plain as Parent\n",
            "ordinary/impl.py": "class Plain:\n    pass\n",
            "unrelated.py": "from pydantic import BaseModel\nclass Parent(BaseModel):\n    pass\n",
        },
        "kind": CLEAN,
    },
    "own_class_named_like_a_library_base_is_not_a_false_red": {
        "base": "from ordinary import BaseModel\nclass M(BaseModel):\n    'old'\n    pass\n",
        "head": "from ordinary import BaseModel\nclass M(BaseModel):\n    'new'\n    pass\n",
        "extra_both": {"ordinary.py": "class BaseModel:\n    pass\n"},
        "kind": CLEAN,
    },
    "property_setter_with_the_same_qualname": {
        "base": (
            "class C:\n    @property\n    def x(self):\n        'old'\n        return 1\n"
            "    @x.setter\n    def x(self, value):\n        'setter'\n        pass\n"
        ),
        "head": (
            "class C:\n    @property\n    def x(self):\n        return 1\n"
            "    @x.setter\n    def x(self, value):\n        pass\n"
        ),
        "kind": CLEAN,
    },
    "type_ignore_moved_across_the_module_docstring": {
        "base": '# type: ignore\n"doc"\nx: int = "bad"\n',
        "head": '"doc"\n# type: ignore\nx: int = "bad"\n',
        "kind": "directive",
    },
    "fmt_off_after_a_removed_function_docstring": {
        "base": 'def f():\n    "doc"\n    # fmt: off\n    return 1\n',
        "head": "def f():\n    # fmt: off\n    return 1\n",
        "kind": CLEAN,
    },
    "fmt_off_after_a_removed_async_and_class_docstring": {
        "base": (
            'async def f():\n    "doc"\n    # fmt: off\n    return 1\n'
            'class C:\n    "doc"\n    # fmt: off\n    y = 1\n'
        ),
        "head": (
            "async def f():\n    # fmt: off\n    return 1\n"
            "class C:\n    # fmt: off\n    y = 1\n"
        ),
        "kind": CLEAN,
    },
    "docstring_with_a_trailing_semicolon": {
        "base": 'def f():\n    "doc";\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": CLEAN,
    },
    "contract_read_through_a_reexport": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "public.py": "from m import f\n",
            "tests/test_doc.py": "from public import f\nassert f.__doc__ == 'old'\n",
        },
        "kind": "contract_docstring",
    },
    "contract_class_through_a_relative_import": {
        "file": "pkg/m.py",
        "base": "from . import Parent\nclass Child(Parent):\n    'old'\n    x: int\n",
        "head": "from . import Parent\nclass Child(Parent):\n    'new'\n    x: int\n",
        "extra_both": {"pkg/__init__.py": "from pydantic import BaseModel as Parent\n"},
        "kind": "contract_docstring",
    },
    "contract_inherited_docstring_read_through_the_child": {
        "base": "class A:\n    'old'\nclass B(A):\n    pass\n",
        "head": "class A:\n    'new'\nclass B(A):\n    pass\n",
        "extra_both": {
            "tests/test_doc.py": "import inspect\nfrom m import B\nassert inspect.getdoc(B)\n"
        },
        "kind": "contract_docstring",
    },
    "contract_route_arguments_through_double_star": {
        "base": '@app.get(**{"path": "/x"})\ndef f():\n    "old"\n    return 1\n',
        "head": '@app.get(**{"path": "/x"})\ndef f():\n    "new"\n    return 1\n',
        "kind": "contract_docstring",
    },
    "method_of_the_same_name_in_another_module_is_not_a_false_red": {
        "base": 'class A:\n    def f(self):\n        "old"\n        return 1\n',
        "head": 'class A:\n    def f(self):\n        "new"\n        return 1\n',
        "extra_both": {
            "other.py": 'class A:\n    def f(self):\n        "used"\n        return 2\n',
            "tests/test_doc.py": "from other import A\nassert A.f.__doc__ == 'used'\n",
        },
        "kind": CLEAN,
    },
    "registration_with_an_explicit_description_is_free": {
        "base": 'def f():\n    "old"\n    return 1\napp.add_api_route("/x", f, description="d")\n',
        "head": 'def f():\n    "new"\n    return 1\napp.add_api_route("/x", f, description="d")\n',
        "kind": CLEAN,
    },
    "registration_out_of_schema_is_free": {
        "base": 'def f():\n    "old"\n    return 1\napp.add_api_route("/x", f, include_in_schema=False)\n',
        "head": 'def f():\n    "new"\n    return 1\napp.add_api_route("/x", f, include_in_schema=False)\n',
        "kind": CLEAN,
    },
    "type_ignore_made_ineffective_by_a_prefix": {
        "base": "x = missing  # type: ignore[name-defined]\n",
        "head": "x = missing  # пояснение # type: ignore[name-defined]\n",
        "kind": "ast",
    },
    "contract_nested_helper_read_in_the_same_scope": {
        "base": (
            "import inspect\ndef test_doc():\n    def helper():\n"
            '        "old"\n        return 1\n    assert inspect.getdoc(helper)\n'
        ),
        "head": (
            "import inspect\ndef test_doc():\n    def helper():\n"
            '        "new"\n        return 1\n    assert inspect.getdoc(helper)\n'
        ),
        "kind": "contract_docstring",
    },
    "contract_reader_reexported_under_another_name": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "doc_api.py": "from inspect import getdoc as read_doc\n",
            "tests/test_doc.py": (
                "from doc_api import read_doc\nfrom m import f\n"
                "assert read_doc(f) == 'old'\n"
            ),
        },
        "kind": "contract_docstring",
    },
    "contract_decorator_reexported_under_another_name": {
        "base": "from decorators import cmd\n@cmd()\ndef f():\n    'old'\n    return 1\n",
        "head": "from decorators import cmd\n@cmd()\ndef f():\n    'new'\n    return 1\n",
        "extra_both": {"decorators.py": "from click import command as cmd\n"},
        "kind": "contract_docstring",
    },
    "contract_read_through_a_module_alias_reexport": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "public.py": "import m as api\n",
            "tests/test_doc.py": "from public import api\nassert api.f.__doc__ == 'old'\n",
        },
        "kind": "contract_docstring",
    },
    "contract_nested_class_method_read_through_a_reexport": {
        "base": "class A:\n    class B:\n        def f(self):\n            'old'\n            return 1\n",
        "head": "class A:\n    class B:\n        def f(self):\n            'new'\n            return 1\n",
        "extra_both": {
            "public.py": "from m import A\n",
            "tests/test_doc.py": "import public\nassert public.A.B.f.__doc__ == 'old'\n",
        },
        "kind": "contract_docstring",
    },
    "contract_read_through_a_long_reexport_chain": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "r0.py": "from r1 import f\n",
            "r1.py": "from r2 import f\n",
            "r2.py": "from r3 import f\n",
            "r3.py": "from r4 import f\n",
            "r4.py": "from m import f\n",
            "r5.py": "from r0 import f\n",
            "tests/test_doc.py": "from r5 import f\nassert f.__doc__ == 'old'\n",
        },
        "kind": "contract_docstring",
    },
    "contract_inherited_through_a_reexported_alias": {
        "base": "class Parent:\n    def f(self):\n        'old'\n        return 1\n",
        "head": "class Parent:\n    def f(self):\n        'new'\n        return 1\n",
        "extra_both": {
            "public.py": "from m import Parent as Public\n",
            "child.py": "from public import Public\nclass Child(Public):\n    pass\n",
            "tests/test_doc.py": (
                "import inspect\nfrom child import Child\n"
                "assert inspect.getdoc(Child.f) == 'old'\n"
            ),
        },
        "kind": "contract_docstring",
    },
    "contract_method_of_a_class_that_is_also_read_directly": {
        "base": "class A:\n    def f(self):\n        'old'\n        return 1\nclass B(A):\n    pass\n",
        "head": "class A:\n    def f(self):\n        'new'\n        return 1\nclass B(A):\n    pass\n",
        "extra_both": {
            "tests/test_doc.py": (
                "import inspect\nfrom m import A, B\n"
                "assert inspect.getdoc(A) is None\nassert inspect.getdoc(B.f) == 'old'\n"
            )
        },
        "kind": "contract_docstring",
    },
    "unparsable_file_makes_the_contract_index_incomplete": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {"tests/test_doc.py": "from m import f\nif True\n"},
        "kind": "error",
    },
    "qualified_pkg_module_is_not_the_root_module": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "pkg/__init__.py": "",
            "pkg/m.py": 'def f():\n    "external"\n    return 1\n',
            "tests/test_doc.py": "import pkg.m\nassert pkg.m.f.__doc__ == 'external'\n",
        },
        "kind": CLEAN,
    },
    "inheritance_is_not_mixed_between_same_named_classes": {
        "base": "class A:\n    'old'\n",
        "head": "class A:\n    'new'\n",
        "extra_both": {
            "other.py": "class A:\n    'x'\nclass B(A):\n    pass\n",
            "tests/test_doc.py": (
                "import inspect\nimport other\nassert inspect.getdoc(other.B)\n"
            ),
        },
        "kind": CLEAN,
    },
    "string_in_an_if_body_changed": {
        "base": 'def f():\n    if True:\n        "code"\n    return 1\n',
        "head": 'def f():\n    if True:\n        "code2"\n    return 1\n',
        "kind": "tokens",
    },
    "late_comment_that_looks_like_a_cookie_is_plain": {
        "base": "x = 1\ny = 2\n",
        "head": "x = 1\n# coding: latin-1 (заметка)\ny = 2\n",
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
