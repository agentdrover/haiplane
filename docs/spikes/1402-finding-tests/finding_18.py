"""#1246: AC surfaces must dispatch a review, not assemble reviewer_prompt."""

from __future__ import annotations

import ast
import inspect

from tests.test_review_evidence import _surfaces


def _called_names(src: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_ac_surfaces_dispatch_review_instead_of_assembling_prompt() -> None:
    names = _called_names(inspect.getsource(_surfaces))
    dispatched = bool(
        names
        & {
            "prepare_review_order",
            "maybe_dispatch_review",
            "dispatch_local_review",
        }
    )
    assert dispatched, (
        "AC _surfaces never dispatches a review; it would stay green if "
        "standing were dropped from prepare_review_order"
    )
    assert "prepass_block" not in names, (
        "AC _surfaces assembles reviewer_prompt itself instead of reading "
        "the dispatched order"
    )
