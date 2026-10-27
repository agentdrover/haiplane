"""Общие помощники тестов листов карточки (#1813)."""

from __future__ import annotations

from tests.test_csrf_templates import Node, _by_id, _controls, parse_html

SHEET_HEADERS = {"HX-Request": "true", "X-Hub-Sheet": "1"}


def sheet_form(page: str, sheet_id: str) -> tuple[Node, dict[str, str]]:
    """Форма листа ``sheet_id`` и то, что отправит браузер без правки пользователя."""
    root = parse_html(page)
    sheet = _by_id(root, sheet_id)
    assert sheet is not None, f"на странице нет листа {sheet_id}"
    form = next(n for n in sheet.walk() if n.tag == "form")
    return form, _controls(form)


def inside(root: Node, node: Node, ident: str) -> bool:
    cur = node
    while cur is not None:
        if cur.attrs.get("id") == ident:
            return True
        cur = cur.parent
    return False
