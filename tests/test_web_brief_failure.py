from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo

from tests.web_support import _web_task_in_review

PASSWORD = "hunter2pass"  # a fake value for the leak check


async def test_brief_failure_reason_is_scrubbed_before_it_reaches_the_card(
    client: AsyncClient, monkeypatch
):
    # #1622 review P1: the cause is shown to a reader, so a git/gh stderr must
    # not carry a credential URL, a bearer header or a host path into the HTML.
    from hub.services import review_brief

    task_id = await _web_task_in_review(client)
    secrets = [
        PASSWORD,
        "tok-abc123",
        "Bearer abcDEF999",
        "/Users/denis/secret-dir",
        "ghp_ZZZ111",
    ]

    async def _leaky(*args, **kwargs):
        raise RuntimeError(
            "fatal: https://user:" + PASSWORD + "@github.com/x/y.git?token=tok-abc123 "
            "Authorization: Bearer abcDEF999 at /Users/denis/secret-dir/repo ghp_ZZZ111"
        )

    monkeypatch.setattr(review_brief, "build_review_brief", _leaky)
    text = (await client.get(f"/tasks/{task_id}")).text

    assert "бриф не собран: RuntimeError" in text
    for secret in secrets:
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
        assert text.count("бриф не собран: RuntimeError: git diff застрял") == 1, fields
