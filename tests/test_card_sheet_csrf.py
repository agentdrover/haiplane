"""CSRF в режиме require для листов и панели (#1813, AC-3)."""

from __future__ import annotations

from tests.card_sheet_support import SHEET_HEADERS
from tests.test_csrf_templates import (  # noqa: F401 - фикстура web
    _closest,
    _refused,
    parse_html,
    requests_of,
    web,
)

SHEET_STATES = ("draft", "review", "needs_decision", "running")


def _sheet_requests(page: str, body_headers: str, path: str):
    out = []
    for req in requests_of(page, body_headers, path):
        node = req["node"]
        sheet = _closest(node, "section")
        if sheet is not None and "c2-sheet" in sheet.attrs.get("class", ""):
            out.append(req)
    return out


async def test_decision_sheets_pass_csrf_require(web):  # noqa: F811
    seen_actions: set[str] = set()
    ids = {}
    for path in web.pages:
        if not path.startswith("/tasks/") or not path[7:].isdigit():
            continue
        row = await web.db.execute_fetchall(
            "SELECT status FROM tasks WHERE id = ?", (int(path[7:]),)
        )
        ids.setdefault(row[0]["status"], path)
    assert all(s in ids for s in SHEET_STATES), ids
    for status in SHEET_STATES:
        path = ids[status]
        page = (await web.browser.get(path)).text
        sheet_reqs = _sheet_requests(page, web.browser.body_headers, path)
        assert sheet_reqs, f"{status}: на странице нет форм листов"
        for req in sheet_reqs:
            seen_actions.add(req["url"].rsplit("/", 1)[-1])
            assert req["data"].get("csrf_token") == web.browser.token
            headers = {**req["headers"], **SHEET_HEADERS}
            ok = await web.browser.send({**req, "headers": headers})
            assert not _refused(ok), (req["url"], ok.text[:200])
            bare = await web.stranger.send({**req, "headers": headers})
            assert _refused(bare), (req["url"], bare.status_code)
    assert {
        "web-approve",
        "web-review-verdict",
        "web-decide",
        "web-return-to-work",
    } <= seen_actions


async def test_bottom_bar_buttons_are_links_not_posts(web):  # noqa: F811
    path = next(p for p in web.pages if p.startswith("/tasks/"))
    page = (await web.browser.get(path)).text
    root = parse_html(page)
    bar = [n for n in root.walk() if "c2-bar" in n.attrs.get("class", "").split()]
    for node in bar:
        for inner in node.walk():
            assert inner.tag != "form" and "hx-post" not in inner.attrs
