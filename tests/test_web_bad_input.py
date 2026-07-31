"""Bad form and query input gets a controlled 4xx, not a 500 (#367).

Two handlers parsed untrusted input inline and let the parse error escape:
``web_create_task`` built ``TaskType(...)`` / ``WorkType(...)`` from form
fields, and ``web_admin_audit`` called ``int()`` on ``?page``. Both raised a
bare ValueError out of the handler, which reaches the client as a 500 — a
server-fault answer to a malformed request.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hub import config
from hub.auth import TokenIdentity
from hub.web import _page_query


@pytest.fixture
def admin_headers(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"admin-token": TokenIdentity("admin-user", "admin")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer admin-token"}


# --- H4: enum fields on the create form ------------------------------------


@pytest.mark.parametrize(
    "form, field",
    [
        ({"title": "t", "task_type": "bad"}, "task_type"),
        ({"title": "t", "task_type": "task", "work_type": "bad"}, "work_type"),
    ],
)
async def test_unknown_enum_value_is_a_bad_request(
    client: AsyncClient, form: dict, field: str
):
    """AC-1. Before the fix: ValueError('bad' is not a valid TaskType) escaped
    the handler."""
    resp = await client.post("/tasks/create", data=form, follow_redirects=False)

    assert resp.status_code == 400, resp.text
    assert field in resp.text, "the answer should name the field that was wrong"


async def test_a_valid_create_form_still_works(client: AsyncClient):
    """The task's constraint: valid input behaves exactly as before."""
    resp = await client.post(
        "/tasks/create",
        data={"title": "a real task", "task_type": "task", "work_type": "feature"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    listed = (await client.get("/api/tasks")).json()
    assert any(t["title"] == "a real task" for t in listed)


# --- H5: the page query param ----------------------------------------------


async def test_non_numeric_page_is_a_bad_request(
    client: AsyncClient, admin_headers: dict
):
    """AC-2. Before the fix: ValueError(invalid literal for int()) escaped the
    handler."""
    resp = await client.get("/admin/audit?page=abc", headers=admin_headers)
    assert resp.status_code == 400, resp.text


async def test_a_valid_page_still_renders(client: AsyncClient, admin_headers: dict):
    for page in ("", "1", "2"):
        resp = await client.get(f"/admin/audit?page={page}", headers=admin_headers)
        assert resp.status_code == 200, f"page={page!r}: {resp.text[:200]}"


@pytest.mark.parametrize("raw, expected", [("0", 1), ("-5", 1), ("", 1), ("3", 3)])
def test_non_positive_pages_fall_back_to_the_first(raw: str, expected: int):
    """The half of this defect that does not crash, and so would survive a fix
    aimed only at the crash.

    ``page=0`` parses fine and yields OFFSET -50, which SQLite silently treats
    as no offset: the handler rendered page 1's rows while the template still
    called it page 0, and the "previous" link pointed at page -1. Verified
    before the fix — both ``?page=0`` and ``?page=-5`` answered 200.
    """

    class _Req:
        query_params = {}

    req = _Req()
    req.query_params = {"page": raw} if raw != "" else {}
    assert _page_query(req) == expected
