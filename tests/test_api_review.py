"""A verdict that sends work back has to say what to redo (#1010).

Observed live on 28.08.2026, task #1005. The reviewer pressed Request Changes
with both form fields empty. Everything mechanical worked: the verdict was
recorded, the task went back to running, the submission stopped being current.
And the developer learned exactly one thing — the work came back. The feed said
"Review verdict: CHANGES_REQUESTED" and nothing else; the PR carried no review
text at all.

Both fields were already on the form and both parameters were already accepted
by the API. Nothing asked for them, so nothing arrived. The neighbouring gates
do ask: DoR refuses a task with no acceptance criteria, submission refuses a
branch it cannot name, delivery refuses a task with no live verdict.

APPROVED is deliberately left free of the requirement — "accepted" is
self-sufficient where "redo it" is not. That asymmetry has a cost of its own,
and it is why the same change files APPROVED under its author: making the
cheaper verdict anonymous is how a gate quietly softens.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _task_in_review(
    client: AsyncClient,
    title: str = "Verdict content",
    headers: dict[str, str] | None = None,
    create_headers: dict[str, str] | None = None,
) -> int:
    """A pair task sitting in client-driven review.

    ``headers`` authenticate the implementer; ``create_headers`` the author of
    the task, because an agent token may not create tasks at all.
    """
    h = headers or {}
    resp = await client.post(
        "/api/tasks", json={"title": title}, headers=create_headers or h
    )
    assert resp.status_code in (200, 201), resp.text
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: do it"},
        headers=h,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "dev", "session_id": "s-1010"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={}, headers=h)
    assert resp.json()["status"] == "review", resp.text
    return task_id


def _principal_tokens() -> dict:
    """Tokens that carry a principal id, so authorship can be observed.

    The default test client authenticates as nobody, which would make the
    authorship assertion below pass for the wrong reason — an anonymous
    verdict from an anonymous caller proves nothing about the route.
    """
    from hub.config import TokenIdentity

    return {
        "impl-token": TokenIdentity("dev", "agent", principal_id=7),
        "reviewer-token": TokenIdentity("denis", "human", principal_id=8),
    }


async def _review_updates(
    client: AsyncClient, task_id: int, headers: dict[str, str] | None = None
) -> list[dict]:
    task = (await client.get(f"/api/tasks/{task_id}", headers=headers or {})).json()
    return [u for u in (task.get("updates") or []) if u.get("kind") == "review"]


# ---- AC-1: an empty CHANGES_REQUESTED is refused, on both paths ----


@pytest.mark.parametrize("path", ["api", "web"])
async def test_changes_requested_without_content_is_refused(
    client: AsyncClient, path: str
):
    """The #1005 case itself: no findings, no comments, work sent back anyway."""
    task_id = await _task_in_review(client)
    before = (await client.get(f"/api/tasks/{task_id}")).json()

    if path == "api":
        resp = await client.post(
            f"/api/tasks/{task_id}/review-verdict",
            json={"verdict": "changes_requested", "agent": "reviewer"},
        )
    else:
        resp = await client.post(
            f"/tasks/{task_id}/web-review-verdict",
            data={"verdict": "changes_requested", "comments": "   "},
            follow_redirects=False,
        )

    assert resp.status_code in (303, 422), resp.text
    if path == "api":
        detail = resp.json()["detail"]
        assert detail["reason"] == "changes_requested_requires_content", detail
        assert "comments" in detail["hint"], (
            "the refusal must say how to proceed, not only that it refused"
        )
    else:
        # The human filled a form; the reason belongs where they were typing.
        assert "review_error=" in resp.headers["location"], resp.headers

    after = (await client.get(f"/api/tasks/{task_id}")).json()
    assert after["status"] == "review", (
        "a refused verdict must not move the task — the reviewer is still holding it"
    )
    assert after["submission_generation"] == before["submission_generation"], (
        "a rejected verdict must not consume the submission it was rejected on"
    )
    assert after["review_cycle"] == before["review_cycle"]
    assert after["latest_review"] is None
    assert not await _review_updates(client, task_id), (
        "nothing was recorded, so the feed must stay silent about it"
    )


# ---- AC-2: content on either path is enough, and it reaches the feed ----


async def test_changes_requested_with_content_is_accepted(client: AsyncClient):
    """One sentence in comments is a reason. Structured findings stay optional."""
    task_id = await _task_in_review(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "Тесты на новую ветку не покрывают отказ",
        },
    )
    assert resp.status_code == 200, resp.text

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "running", "work goes back to the developer as before"
    assert task["review_cycle"] == 1
    updates = await _review_updates(client, task_id)
    assert updates, "the verdict must be in the feed"
    assert "Тесты на новую ветку не покрывают отказ" in updates[0]["content"], (
        "the reason has to be visible where the developer reads, not only in the row"
    )


async def test_changes_requested_with_only_a_finding_is_accepted(client: AsyncClient):
    """The other half of the OR: a finding with no comments text."""
    task_id = await _task_in_review(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {
                    "id": 1,
                    "severity": "high",
                    "message": "race in bump",
                    "scope": "in_scope",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["status"] == "running"
    assert task["latest_review"]["findings"], resp.text


# ---- AC-3: APPROVED needs no content, but is never anonymous ----


async def test_approved_needs_no_content_but_is_attributed(
    client: AsyncClient, monkeypatch
):
    """#1005's verdict was filed with author_kind=anonymous, from the web form.

    The API route passed principal_id and the web route did not, so the one
    verdict a human submits by hand was the one with no author. Approve stays
    free to be wordless — it must not also be nameless, or the softer verdict
    becomes the cheaper one in every sense.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    reviewer = {"Authorization": "Bearer reviewer-token"}

    task_id = await _task_in_review(
        client,
        headers={"Authorization": "Bearer impl-token"},
        create_headers=reviewer,
    )

    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "", "findings_text": ""},
        headers=reviewer,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    task = (await client.get(f"/api/tasks/{task_id}", headers=reviewer)).json()
    assert task["status"] == "running"
    assert task["review_approved_current"] is True, (
        "a wordless approve is still a valid approve"
    )
    updates = await _review_updates(client, task_id, headers=reviewer)
    assert updates, "an approve must reach the feed too, not only the task row"
    assert updates[0]["author_kind"] != "anonymous", (
        f"the verdict must name its author, got {updates[0]['author_kind']!r}"
    )
    assert updates[0]["principal_id"] is not None


# ---- the form asks before the server refuses ----


async def test_review_form_warns_before_an_empty_request_changes(client: AsyncClient):
    """AC-4's automatable half: the hint and the guard ship with the form.

    The browser behaviour itself is checked by hand on the demo stand; what a
    test can pin is that the page carries the check at all, so a later edit to
    the template cannot drop it silently.
    """
    task_id = await _task_in_review(client)

    page = (await client.get(f"/tasks/{task_id}")).text

    assert 'id="review-cr-hint"' in page
    assert "changes_requested" in page
    assert "review-verdict-form" in page, (
        "the guard hangs off the form id; without it the listener matches nothing"
    )


# ---- #1057: a verdict is read back to itself before it is written ----
#
# 29.08.2026 gave two input errors on one day from the same form. #1042 12:41
# was a byte-for-byte re-paste of the 12:21 verdict whose complaint the author
# had already closed; #1041 06:33 was filed CHANGES_REQUESTED over a text
# opening with the word APPROVED, which the reviewer admitted in his next
# verdict. The hub took both without a word.


async def _verdict(client: AsyncClient, task_id: int, **body):
    return await client.post(f"/api/tasks/{task_id}/review-verdict", json=body)


async def _back_in_review(client: AsyncClient, task_id: int) -> None:
    """After a recorded verdict the task returns to running — resubmit it."""
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.json()["status"] == "review", resp.text


async def test_a_verdict_repeating_the_previous_text_is_refused(client: AsyncClient):
    """#1057 AC-1: the same words twice, and the second time unannounced."""
    task_id = await _task_in_review(client, title="Repeat")
    text = "Сдан 0f4fec5, вершина ветки 4f44311. Пересдайте с вершиной."
    resp = await _verdict(
        client, task_id, verdict="changes_requested", comments=text, agent="rev"
    )
    assert resp.status_code == 200, resp.text
    await _back_in_review(client, task_id)

    before = (await client.get(f"/api/tasks/{task_id}")).json()
    resp = await _verdict(
        client,
        task_id,
        verdict="changes_requested",
        # The re-paste's only difference from the original, as on #1042.
        comments=text.replace("\n", "\r\n") + "  ",
        agent="rev",
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "verdict_repeats_previous"
    after = (await client.get(f"/api/tasks/{task_id}")).json()
    assert after["status"] == "review", "a refused verdict does not move the task"
    assert after["submission_generation"] == before["submission_generation"], (
        "and does not consume the submission it was refused on"
    )


async def test_a_deliberate_repeat_is_accepted(client: AsyncClient):
    """#1057 AC-2: saying it twice is right when it was not fixed twice."""
    task_id = await _task_in_review(client, title="Meant it")
    text = "Тесты по AC-2 всё ещё красные — тот же дефект, тот же ответ."
    assert (
        await _verdict(
            client, task_id, verdict="changes_requested", comments=text, agent="rev"
        )
    ).status_code == 200
    await _back_in_review(client, task_id)

    resp = await _verdict(
        client,
        task_id,
        verdict="changes_requested",
        comments=text,
        agent="rev",
        acknowledge_repeat=True,
    )

    assert resp.status_code == 200, resp.text
    verdicts = await _review_updates(client, task_id)
    assert len(verdicts) == 2, "the repeat is recorded, not swallowed"


async def test_a_verdict_contradicting_its_own_text_is_refused(client: AsyncClient):
    """#1057 AC-3: two outcomes in one call is a typo, not a decision."""
    task_id = await _task_in_review(client, title="Contradiction")

    resp = await _verdict(
        client,
        task_id,
        verdict="changes_requested",
        comments="APPROVED. Правка ровно та, что просили.",
        agent="rev",
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "verdict_contradicts_its_text"
    assert not await _review_updates(client, task_id), "nothing was written"

    # The mirror case is refused too — this is not an anti-APPROVED rule.
    resp = await _verdict(
        client,
        task_id,
        verdict="approved",
        comments="CHANGES_REQUESTED: тесты красные.",
        agent="rev",
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "verdict_contradicts_its_text"


async def test_quoting_the_word_approved_is_not_a_contradiction(client: AsyncClient):
    """#1057 AC-4: the check reads a declaration, not any occurrence."""
    task_id = await _task_in_review(client, title="Quoting")

    resp = await _verdict(
        client,
        task_id,
        verdict="changes_requested",
        comments=(
            "Разбор прошлой сдачи в силе.\n"
            "Вердикт APPROVED от 06:33 был ошибкой ввода — здесь это не он.\n"
            "Блокирует одно: PR разошёлся с develop."
        ),
        agent="rev",
    )

    assert resp.status_code == 200, resp.text
    assert len(await _review_updates(client, task_id)) == 1


async def test_a_verdict_with_no_body_is_not_a_repeat(client: AsyncClient):
    """A bare APPROVED is the common case and must never trip the check."""
    task_id = await _task_in_review(client, title="Bare approvals")
    assert (
        await _verdict(client, task_id, verdict="approved", agent="rev")
    ).status_code == 200
    await _back_in_review(client, task_id)

    resp = await _verdict(client, task_id, verdict="approved", agent="rev")

    assert resp.status_code == 200, resp.text
