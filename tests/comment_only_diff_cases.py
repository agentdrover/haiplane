"""Таблица пар «было → стало» для tests/test_comment_only_diff.py (#2032).

Строгий режим: разрешены только правки комментариев и docstring-и, названные в
``allow``. Запись — пара одного файла (``file``, по умолчанию m.py) плюс
неизменные соседи ``extra_both`` и ожидаемый исход: ``CLEAN`` — rc 0, имя вида —
rc 1 с ``[вид]`` в выводе, ``"error"`` — rc 2. ``allow`` — значения
``--allow-docstring``.
"""

from __future__ import annotations

CLEAN = None

CASES: dict[str, dict] = {
    # --- чистые правки ---------------------------------------------------
    "clean_comment_edit_cyrillic": {
        "base": 'x = 1  # старый комментарий\n# история #12\nname = "кириллица"\n',
        "head": 'x = 1  # новый\n# #12 причина\nname = "кириллица"\n',
        "kind": CLEAN,
    },
    "clean_comment_lines_and_blank_lines_come_and_go": {
        "base": "# a\n\nx = 1\n\n\n# b\ny = 2\n",
        "head": "x = 1\ny = 2\n# c\n",
        "kind": CLEAN,
    },
    "clean_trailing_comment_removed_with_its_own_trailing_spaces": {
        "base": "x = 1  # c   \ny = 2\n",
        "head": "x = 1\ny = 2\n",
        "kind": CLEAN,
    },
    "clean_comment_inside_brackets": {
        "base": "call(\n    1,  # c\n    # note\n    2,\n)\n",
        "head": "call(\n    1,\n    2,  # d\n)\n",
        "kind": CLEAN,
    },
    "clean_comment_after_a_multiline_string": {
        "base": 'x = """a\n\nb"""  # old\n',
        "head": 'x = """a\n\nb"""\n',
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
        "allow_files": ["docs/agent-context/invariants.md"],
        "kind": CLEAN,
    },
    "clean_allowed_docstring_change": {
        "base": 'def f():\n    """Длинно."""\n    return 1\n',
        "head": 'def f():\n    """Кратко."""\n    return 1\n',
        "allow": ["m.py::f"],
        "kind": CLEAN,
    },
    "clean_allowed_docstring_removed_and_module_docstring": {
        "base": '"""old"""\nclass A:\n    """old"""\n    y = 1\n',
        "head": "class A:\n    y = 1\n",
        "allow": ["m.py::<module>", "m.py::A"],
        "kind": CLEAN,
    },
    "clean_allowed_bracketed_and_concatenated_docstring": {
        "base": 'def f():\n    ("a"\n     "b")\n    return 1\n',
        "head": 'def f():\n    """x"""\n    return 1\n',
        "allow": ["m.py::f"],
        "kind": CLEAN,
    },
    "clean_allowed_property_setter_with_the_same_qualname": {
        "base": (
            "class C:\n    @property\n    def x(self):\n        'old'\n        return 1\n"
            "    @x.setter\n    def x(self, value):\n        'setter'\n        pass\n"
        ),
        "head": (
            "class C:\n    @property\n    def x(self):\n        return 1\n"
            "    @x.setter\n    def x(self, value):\n        pass\n"
        ),
        "allow": ["m.py::C.x"],
        "kind": CLEAN,
    },
    # --- код ---------------------------------------------------------------
    "code_changed": {
        "base": "def f():\n    return 1\n",
        "head": "def f():\n    return 2\n",
        "kind": "code",
    },
    "plain_string_changed": {
        "base": 'x = "a"\n',
        "head": 'x = "b"\n',
        "kind": "code",
    },
    "fstring_changed": {
        "base": 'x = f"a{y}"\n',
        "head": 'x = f"b{y}"\n',
        "kind": "code",
    },
    "fstring_is_not_a_docstring": {
        "base": 'def f():\n    f"doc"\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": "code",
    },
    "bytes_is_not_a_docstring": {
        "base": 'def f():\n    b"doc"\n    return 1\n',
        "head": "def f():\n    return 1\n",
        "kind": "code",
    },
    "second_string_is_not_a_docstring": {
        "base": 'def f():\n    x = 1\n    "later"\n    return x\n',
        "head": "def f():\n    x = 1\n    return x\n",
        "kind": "code",
    },
    "string_in_an_if_body_changed": {
        "base": 'def f():\n    if True:\n        "code"\n    return 1\n',
        "head": 'def f():\n    if True:\n        "code2"\n    return 1\n',
        "kind": "code",
    },
    "empty_body_after_docstring_removal": {
        "base": 'def f():\n    """doc"""\n',
        "head": "def f():\n    pass\n",
        "allow": ["m.py::f"],
        "kind": "code",
    },
    "blank_line_inside_a_string_is_content": {
        "base": 'x = """a\n\nb"""\n',
        "head": 'x = """a\nb"""\n',
        "kind": "code",
    },
    "trailing_space_inside_a_string_is_content": {
        "base": 'x = """a  \nb"""\n',
        "head": 'x = """a\nb"""\n',
        "kind": "code",
    },
    "blank_line_inside_a_multiline_fstring_is_content": {
        "base": 'x = f"""a {y}\n\nb"""\n',
        "head": 'x = f"""a {y}\nb"""\n',
        "kind": "code",
    },
    "trailing_space_on_a_code_line_is_significant": {
        "base": "y = 2   \n",
        "head": "y = 2\n",
        "kind": "code",
    },
    "other_changed_file_is_unlisted_whatever_its_type": {
        "base": "x = 1\n",
        "head": "x = 1\n",
        "extra_head": {"docs/note.md": "q\n"},
        "kind": "unlisted_file",
    },
    "indent_changed": {
        "base": "def f():\n    x = 1\n    return x\n",
        "head": "def f():\n  x = 1\n  return x\n",
        "kind": "code",
    },
    "line_endings_changed": {
        "base": "x = 1\ny = 2\n",
        "head": "x = 1\r\ny = 2\r\n",
        "kind": "code",
    },
    # --- находки Codex, круг 5: перенос строк кода --------------------------
    "expression_wrapped_away_from_its_noqa": {
        "base": "x = (missing1 + missing2  # noqa: F821\n)\n",
        "head": "x = (missing1 +\n     missing2  # noqa: F821\n)\n",
        "kind": "code",
    },
    "expression_wrapped_without_directives": {
        "base": "x = [1, 2, 3]\n",
        "head": "x = [1,\n     2, 3]\n",
        "kind": "code",
    },
    "lines_joined": {
        "base": "x = (1 +\n     2)\n",
        "head": "x = (1 + 2)\n",
        "kind": "code",
    },
    # --- директивы -----------------------------------------------------------
    "noqa_removed": {
        "base": "x = 1  # noqa: BLE001\n",
        "head": "x = 1\n",
        "kind": "code",
    },
    "noqa_codes_changed": {
        "base": "x = 1  # noqa: BLE001\n",
        "head": "x = 1  # noqa: E501\n",
        "kind": "code",
    },
    "noqa_code_got_a_suffix": {
        "base": "x = foo  # noqa: F821\n",
        "head": "x = foo  # noqa: F821XYZ\n",
        "kind": "code",
    },
    "noqa_added": {
        "base": "x = 1\n",
        "head": "x = 1  # noqa\n",
        "kind": "code",
    },
    "noqa_moved_to_neighbour_statement": {
        "base": "x = 1  # noqa: E501\ny = 2\n",
        "head": "x = 1\ny = 2  # noqa: E501\n",
        "kind": "code",
    },
    "noqa_moved_to_other_line_of_one_statement": {
        "base": "call(\n    1,  # noqa: E501\n    2,\n)\n",
        "head": "call(\n    1,\n    2,  # noqa: E501\n)\n",
        "kind": "code",
    },
    "type_ignore_removed": {
        "base": "x: int = f()  # type: ignore[arg-type]\n",
        "head": "x: int = f()\n",
        "kind": "code",
    },
    "type_ignore_case_changed": {
        "base": "x = f()  # type: ignore[name-defined]\n",
        "head": "x = f()  # type: ignore[NAME-DEFINED]\n",
        "kind": "code",
    },
    "type_comment_removed": {
        "base": "x = []  # type: list[int]\n",
        "head": "x = []\n",
        "kind": "code",
    },
    "pragma_removed": {
        "base": "def f():  # pragma: no cover\n    return 1\n",
        "head": "def f():\n    return 1\n",
        "kind": "code",
    },
    "allowlist_secret_removed": {
        "base": 'k = "abc"  # pragma: allowlist secret\n',
        "head": 'k = "abc"\n',
        "kind": "code",
    },
    "fmt_skip_removed": {
        "base": "x   =   1  # fmt: skip\n",
        "head": "x   =   1\n",
        "kind": "code",
    },
    "nosec_ids_changed": {
        "base": "x = 1  # nosec: B101\n",
        "head": "x = 1  # nosec: B102\n",
        "kind": "code",
    },
    "directive_order_swapped": {
        "base": "# fmt: off\n# fmt: on\nx = 1\n",
        "head": "# fmt: on\n# fmt: off\nx = 1\n",
        "kind": "code",
    },
    "fmt_off_indent_changed": {
        "base": "def f():\n    x   = 1\n    # fmt: off\ny   = 2\n",
        "head": "def f():\n    x   = 1\n# fmt: off\ny   = 2\n",
        "kind": "code",
    },
    "directive_prefixed_by_prose_stops_working": {
        "base": "x = missing  # type: ignore[name-defined]\n",
        "head": "x = missing  # пояснение # type: ignore[name-defined]\n",
        "kind": "code",
    },
    "shebang_removed": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": "x = 1\n",
        "kind": "code",
    },
    "shebang_indented": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": " #!/usr/bin/env python3\nx = 1\n",
        "kind": "code",
    },
    "coding_cookie_removed": {
        "base": "# -*- coding: utf-8 -*-\nx = 1\n",
        "head": "x = 1\n",
        "kind": "code",
    },
    "coding_cookie_moved_after_code": {
        "base": '# coding: utf-8\n"""d"""\nx = 1\n',
        "head": '"""d"""\n# coding: utf-8\nx = 1\n',
        "allow": ["m.py::<module>"],
        "kind": "code",
    },
    "late_comment_that_looks_like_a_cookie_is_plain": {
        "base": "x = 1\ny = 2\n",
        "head": "x = 1\n# coding: latin-1 (заметка)\ny = 2\n",
        "kind": CLEAN,
    },
    "type_ignore_moved_across_the_module_docstring": {
        "base": '# type: ignore\n"doc"\nx: int = "bad"\n',
        "head": '"doc"\n# type: ignore\nx: int = "bad"\n',
        "allow": ["m.py::<module>"],
        "kind": "code",
    },
    "fmt_off_after_a_removed_function_docstring": {
        "base": 'def f():\n    "doc"\n    # fmt: off\n    return 1\n',
        "head": "def f():\n    # fmt: off\n    return 1\n",
        "allow": ["m.py::f"],
        "kind": CLEAN,
    },
    "directive_on_a_docstring_line_must_stay": {
        "base": 'def f():\n    """doc"""  # noqa: D400\n    return 1\n',
        "head": 'def f():\n    """doc"""\n    return 1\n',
        "kind": "code",
    },
    # --- docstring: красный по умолчанию -----------------------------------
    "plain_function_docstring_changed": {
        "base": 'def g():\n    """A."""\n    return 1\n',
        "head": 'def g():\n    """B."""\n    return 1\n',
        "kind": "docstring",
    },
    "docstring_removed": {
        "base": 'def g():\n    """A."""\n    return 1\n',
        "head": "def g():\n    return 1\n",
        "kind": "docstring",
    },
    "docstring_added": {
        "base": "def g():\n    return 1\n",
        "head": 'def g():\n    """A."""\n    return 1\n',
        "kind": "docstring",
    },
    "module_and_class_docstrings_changed": {
        "base": '"""old"""\nclass A:\n    """old"""\n    y = 1\n',
        "head": "class A:\n    y = 1\n",
        "kind": "docstring",
    },
    "docstring_changed_for_another_symbol_allowed": {
        "base": 'def g():\n    """A."""\n    return 1\n',
        "head": 'def g():\n    """B."""\n    return 1\n',
        "allow": ["m.py::other"],
        "kind": "docstring",
    },
    "mcp_tool_docstring_changed": {
        "base": '@mcp.tool()\ndef hub_t():\n    """Описание."""\n    return 1\n',
        "head": '@mcp.tool()\ndef hub_t():\n    """Иначе."""\n    return 1\n',
        "kind": "docstring",
    },
    "pydantic_model_docstring_changed": {
        "base": 'class M(BaseModel):\n    """Схема."""\n    x: int\n',
        "head": 'class M(BaseModel):\n    """Иначе."""\n    x: int\n',
        "kind": "docstring",
    },
    "argparse_module_docstring_changed": {
        "base": '"""Справка."""\nimport argparse\np = argparse.ArgumentParser(description=__doc__)\n',
        "head": '"""Другая."""\nimport argparse\np = argparse.ArgumentParser(description=__doc__)\n',
        "kind": "docstring",
    },
    # --- четыре сценария Codex по гену 1: контрактность не нужна ----------------
    "decorator_assigned_to_a_name": {
        "base": ("publish = mcp.tool\n@publish()\ndef f():\n    'old'\n    return 1\n"),
        "head": ("publish = mcp.tool\n@publish()\ndef f():\n    'new'\n    return 1\n"),
        "kind": "docstring",
    },
    "model_base_assigned_to_an_alias": {
        "base": "Alias = BaseModel\nclass M(Alias):\n    'old'\n    x: int\n",
        "head": "Alias = BaseModel\nclass M(Alias):\n    'new'\n    x: int\n",
        "kind": "docstring",
    },
    "function_read_through_a_variable": {
        "base": "import inspect\ndef f():\n    'old'\n    return 1\ng = f\nassert inspect.getdoc(g)\n",
        "head": "import inspect\ndef f():\n    'new'\n    return 1\ng = f\nassert inspect.getdoc(g)\n",
        "kind": "docstring",
    },
    "star_reexport_read_by_a_test": {
        "base": 'def f():\n    "old"\n    return 1\n',
        "head": 'def f():\n    "new"\n    return 1\n',
        "extra_both": {
            "public.py": "from m import *\n",
            "tests/test_doc.py": "from public import f\nassert f.__doc__ == 'old'\n",
        },
        "kind": "docstring",
    },
    "nested_class_docstring_inherited_by_a_sibling": {
        "base": (
            "import inspect\nclass Outer:\n    class A:\n        'old'\n"
            "    class B(A):\n        pass\nassert inspect.getdoc(Outer.B)\n"
        ),
        "head": (
            "import inspect\nclass Outer:\n    class A:\n        'new'\n"
            "    class B(A):\n        pass\nassert inspect.getdoc(Outer.B)\n"
        ),
        "kind": "docstring",
    },
    # --- ошибки и состав файлов -----------------------------------------------
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
    "docstring_sharing_a_line_with_code_is_unsupported": {
        "base": 'def f(): "док"; return 2\n',
        "head": "def f(): return 2\n",
        "kind": "error",
    },
    "cyrillic_docstring_followed_by_code_on_its_line": {
        "base": '"дд";\nx = 1\n',
        "head": "x = 1\n",
        "kind": "error",
    },
    "directive_comment_is_compared_verbatim": {
        "base": "x = 1  # история  # noqa: BLE001\n",
        "head": "x = 1  # noqa: BLE001\n",
        "kind": "code",
    },
    "pragma_spacing_changes_its_action": {
        "base": 'key = "abc"  # pragma: allowlist secret\n',
        "head": 'key = "abc"  # pragma:  allowlist secret\n',
        "kind": "code",
    },
    "mypy_directive_loses_its_line_start": {
        "base": '# mypy: ignore-errors\nx: int = "bad"\n',
        "head": '# note # mypy: ignore-errors\nx: int = "bad"\n',
        "kind": "code",
    },
    "nextline_directive_separated_by_a_comment": {
        "base": '# pragma: allowlist nextline secret\nkey = "abc"\n',
        "head": '# pragma: allowlist nextline secret\n# note\nkey = "abc"\n',
        "kind": "code",
    },
    "nextline_directive_separated_by_a_blank_line": {
        "base": '# pragma: allowlist nextline secret\nkey = "abc"\n',
        "head": '# pragma: allowlist nextline secret\n\nkey = "abc"\n',
        "kind": "code",
    },
    "pragma_without_colon_removed": {
        "base": "def f():  # pragma no cover\n    return 1\n",
        "head": "def f():\n    return 1\n",
        "kind": "code",
    },
    "nosec_glued_to_the_id_removed": {
        "base": "assert True  # nosecB101\n",
        "head": "assert True\n",
        "kind": "code",
    },
    "docstring_moved_between_same_named_defs": {
        "base": 'def f():\n    "doc"\n    pass\ndef f():\n    pass\n',
        "head": 'def f():\n    pass\ndef f():\n    "doc"\n    pass\n',
        "kind": "docstring",
    },
    "docstring_moved_from_property_getter_to_setter": {
        "base": (
            "class C:\n    @property\n    def x(self):\n        'doc'\n        return 1\n"
            "    @x.setter\n    def x(self, v):\n        pass\n"
        ),
        "head": (
            "class C:\n    @property\n    def x(self):\n        return 1\n"
            "    @x.setter\n    def x(self, v):\n        'doc'\n        pass\n"
        ),
        "kind": "docstring",
    },
    "directive_moved_inside_a_bracketed_docstring": {
        "base": 'def f():\n    (\n        "Doc"  # noqa: D400\n    )\n    return 1\n',
        "head": 'def f():\n    (\n        "Doc"\n    )  # noqa: D400\n    return 1\n',
        "allow": ["m.py::f"],
        "kind": "code",
    },
    "directive_text_cannot_imitate_the_gap_mark": {
        "base": '# pragma: allowlist nextline secret <зазор 1>\nkey = "abc"\n',
        "head": '# pragma: allowlist nextline secret\n# plain\nkey = "abc"\n',
        "kind": "code",
    },
    "shebang_trailing_nbsp": {
        "base": "#!/usr/bin/env python3\nx = 1\n",
        "head": "#!/usr/bin/env python3\u00a0\nx = 1\n",
        "kind": "code",
    },
    "nosem_removed": {
        "base": "dangerous()  # nosem\n",
        "head": "dangerous()\n",
        "kind": "code",
    },
    "comment_inserted_after_a_backslash_continuation": {
        "base": "x = missing \\\n# noqa: F821\ny = 1\n",
        "head": "x = missing \\\n# ordinary\n# noqa: F821\ny = 1\n",
        "kind": "code",
    },
    "comment_inserted_after_a_directive_on_a_docstring_line": {
        "base": 'def f():\n    "doc"  # pylint: disable-next=undefined-variable\n    return missing\n',
        "head": 'def f():\n    "doc"  # pylint: disable-next=undefined-variable\n    # ordinary\n    return missing\n',
        "kind": "code",
    },
    "comment_inserted_after_a_trailing_directive": {
        "base": "x = 1  # pylint: disable-next=undefined-variable\ny = missing\n",
        "head": "x = 1  # pylint: disable-next=undefined-variable\n# ordinary\ny = missing\n",
        "kind": "code",
    },
    "ty_ignore_moved_to_the_neighbour": {
        "base": 'x: int = "bad"  # ty: ignore[invalid-assignment]\ny: int = "bad"\n',
        "head": 'x: int = "bad"\ny: int = "bad"  # ty: ignore[invalid-assignment]\n',
        "kind": "code",
    },
    "comment_removed_inside_a_bracketed_docstring_before_nextline": {
        "base": 'def f():\n    (\n        # pragma: allowlist nextline secret\n        # ordinary\n        "secret"\n    )\n    pass\n',
        "head": 'def f():\n    (\n        # pragma: allowlist nextline secret\n        "secret"\n    )\n    pass\n',
        "allow": ["m.py::f"],
        "kind": "code",
    },
    "docstring_escape_spelling_changed_without_allow": {
        "base": 'def f():\n    """doc\n    \\x23 pragma: allowlist nextline secret\n    secret\n    """\n    pass\n',
        "head": 'def f():\n    """doc\n    # pragma: allowlist nextline secret\n    secret\n    """\n    pass\n',
        "kind": "docstring",
    },
    "docstring_quotes_changed_without_allow": {
        "base": 'def f():\n    "doc"\n    pass\n',
        "head": "def f():\n    'doc'\n    pass\n",
        "kind": "docstring",
    },
    "directive_prefix_inside_a_bracketed_docstring_is_exact": {
        "base": '(\n # pragma: allowlist nextline secret\n "secret"\n)\nx = 1\n',
        "head": '(\n\f# pragma: allowlist nextline secret\n "secret"\n)\nx = 1\n',
        "allow": ["m.py::<module>"],
        "kind": "code",
    },
    "gitleaks_allow_moved_to_the_neighbour": {
        "base": 'a = "one"  # gitleaks:allow\nb = "two"\n',
        "head": 'a = "one"\nb = "two"  # gitleaks:allow\n',
        "kind": "code",
    },
    "pep723_dependency_pin_changed": {
        "base": '# /// script\n# dependencies = ["requests==2.31.0"]\n# ///\nimport requests\n',
        "head": '# /// script\n# dependencies = ["requests==2.32.0"]\n# ///\nimport requests\n',
        "kind": "code",
    },
    "pep723_block_removed": {
        "base": '# /// script\n# requires-python = ">=3.11"\n# ///\nimport os\n',
        "head": "import os\n",
        "kind": "code",
    },
    "pep723_marker_inside_a_multiline_value": {
        "base": '# /// script\n# [tool.example]\n# note = """\n# ///\n# """\n# [tool.uv]\n# index-url = "https://pypi.org/simple"\n# ///\nprint(1)\n',
        "head": '# /// script\n# [tool.example]\n# note = """\n# ///\n# """\n# [tool.uv]\n# index-url = "https://example.com/simple"\n# ///\nprint(1)\n',
        "kind": "code",
    },
    "pyrefly_ignore_removed": {
        "base": 'x: int = "bad"  # pyrefly: ignore[bad-assignment]\n',
        "head": 'x: int = "bad"\n',
        "kind": "code",
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
