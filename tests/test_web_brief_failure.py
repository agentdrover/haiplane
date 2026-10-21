from __future__ import annotations

import asyncio
import sqlite3
import subprocess

import pytest
from httpx import AsyncClient

from hub import repository as repo

from tests.web_support import _web_task_in_review

PASSWORD = "hunter2pass"  # fake values for the leak check  # pragma: allowlist secret
B64 = "dXNlcjpodW50ZXIycGFzcw=="
LEAKY = (
    "fatal: https://user:" + PASSWORD + "@github.com/x/y.git?token=tok-abc123 "
    "Authorization: Bearer abcDEF999 Authorization: Basic " + B64 + " "
    "{'password': '" + PASSWORD + "'} "
    "git.internal:8443/x?access_token=at-777 10.1.2.3:2222 "
    "at /Users/denis/secret-dir/repo ghp_ZZZ111 line1\\nline2 "
    "eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop"  # pragma: allowlist secret
)
SECRETS = [
    PASSWORD,
    "tok-abc123",
    "abcDEF999",
    B64,
    "at-777",
    "git.internal",
    "10.1.2.3",
    "/Users/denis",
    "ghp_ZZZ111",
    "eyJhbGciOiJIUzI1NiJ9",
    "line2",
]


@pytest.mark.parametrize(
    ("exc", "word"),
    [
        (RuntimeError(LEAKY), "внутренняя ошибка"),
        (TimeoutError(LEAKY), "таймаут"),
        (asyncio.TimeoutError(LEAKY), "таймаут"),
        (subprocess.CalledProcessError(128, "git", stderr=LEAKY), "ошибка git"),
        (OSError(LEAKY), "ошибка git"),
        (sqlite3.OperationalError(LEAKY), "ошибка базы"),
    ],
)
async def test_brief_failure_card_never_shows_the_exception_text(
    client: AsyncClient, monkeypatch, exc, word
):
    # #1622: the cause is a whitelist (type name + fixed category word); the
    # exception text, where git/gh stderr puts credentials, never reaches HTML.
    from hub.services import review_brief

    task_id = await _web_task_in_review(client)

    async def _leaky(*args, **kwargs):
        raise exc

    monkeypatch.setattr(review_brief, "build_review_brief", _leaky)
    text = (await client.get(f"/tasks/{task_id}")).text

    assert f"бриф не собран: {type(exc).__name__} ({word})" in text
    assert "подробности в логе хаба" in text
    for secret in SECRETS:
        assert secret not in text, secret


async def test_brief_failure_reason_shows_without_review_board(
    client: AsyncClient, monkeypatch, db
):
    # #1622 review P2: fix_requested and review with a live review job have no
    # review board, but gate_evidence still assembles there, so the cause shows.
    from hub.services import review_brief

    task_id = await _web_task_in_review(client)

    async def _boom(*args, **kwargs):
        raise RuntimeError("git diff застрял")

    monkeypatch.setattr(review_brief, "build_review_brief", _boom)
    for fields in (
        {"status": "fix_requested", "job_id": "job-fix-1"},
        {"status": "review", "review_job_id": "rj-1"},
    ):
        await repo.update_task(db, task_id, **fields)
        await db.commit()
        text = (await client.get(f"/tasks/{task_id}")).text
        assert text.count("бриф не собран: RuntimeError (внутренняя ошибка)") == 1, (
            fields
        )
