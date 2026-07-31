"""Logout ends the server-side session, not just the cookie (#368).

Before this, ``/logout`` deleted the cookie and left the ``browser_sessions``
row live. A session token captured beforehand kept authenticating for the
rest of its lifetime — up to 30 days — so logging out did nothing at all to
a session someone else already had.
"""

from __future__ import annotations

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import config
from hub.services import admin as admin_svc

TEST_PASSWORD = "correct-horse-battery-staple"


async def _user_with_session(
    db: aiosqlite.Connection, username: str
) -> tuple[int, str]:
    principal = await admin_svc.create_principal(
        db,
        kind="human",
        username=username,
        password=TEST_PASSWORD,
        role_slug="operator",
    )
    token = await admin_svc.create_browser_session(db, principal["id"])
    return principal["id"], token


@pytest.mark.asyncio
async def test_logout_stops_the_session_token_from_authenticating(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. Reproduced before the fix: the token still resolved to an
    identity after logout and revoked_at stayed NULL."""
    _, token = await _user_with_session(db, "alice")
    assert await admin_svc.resolve_browser_session(db, token) is not None

    client.cookies.set(config.HUB_COOKIE_NAME, token)
    resp = await client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303

    assert await admin_svc.resolve_browser_session(db, token) is None, (
        "a session token must stop working the moment its owner logs out"
    )
    rows = await db.execute_fetchall(
        "SELECT revoked_at FROM browser_sessions WHERE principal_id = "
        "(SELECT id FROM principals WHERE username = 'alice')"
    )
    assert dict(rows[0])["revoked_at"] is not None


@pytest.mark.asyncio
async def test_logout_on_one_device_leaves_the_other_signed_in(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The task's constraint: revoke the CURRENT session, not all of them.

    This is the test that catches following the task's own hint literally.
    That hint points at ``_revoke_all_sessions`` as if it were ready to use;
    it closes every session the principal has, so signing out of a laptop
    would sign the same person out of their phone.
    """
    bob_id, laptop = await _user_with_session(db, "bob")
    phone = await admin_svc.create_browser_session(db, bob_id)

    client.cookies.set(config.HUB_COOKIE_NAME, laptop)
    await client.post("/logout", follow_redirects=False)

    assert await admin_svc.resolve_browser_session(db, laptop) is None
    assert await admin_svc.resolve_browser_session(db, phone) is not None, (
        "logging out of one device must not sign the user out everywhere"
    )


@pytest.mark.asyncio
async def test_logout_without_a_cookie_still_reaches_the_login_page(
    client: AsyncClient,
):
    """Logging out is how a user leaves. It must never strand them, whether
    or not there is a session to close."""
    resp = await client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_revoking_twice_is_not_an_error(db: aiosqlite.Connection):
    """A double logout — two tabs, a retried request — closes nothing the
    second time and says so, rather than raising."""
    _, token = await _user_with_session(db, "carol")

    assert await admin_svc.revoke_browser_session(db, token) is True
    assert await admin_svc.revoke_browser_session(db, token) is False
    assert await admin_svc.revoke_browser_session(db, "never-issued") is False
