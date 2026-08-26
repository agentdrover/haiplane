"""A machine review of one's own work is never mistaken for an independent one (#728).

The human-facing path has had the guard since #318/#433: hub_submit_review
refuses a verdict from the implementer, and the brief warns before the effort
is spent. The machine path had none, and the operator said plainly what that
bought — "для аудита слабо, для пропуска в очередь — ок". A guard on one of two
twin doors documents an intent without enforcing it.

The deeper half, found by reading the code: maybe_auto_verdict checks that the
reviewer's MODEL differs from the implementer's (#758) but never that the
PRINCIPAL does. So the implementer could review their own work under another
model declaration and the policy would sign it off as independent.

The baseline is imported from the auto-verdict suite on purpose: these tests
must ride on exactly the same clean grounds that suite defines, so a change
there moves them too instead of leaving a private copy quietly out of date.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.services.orchestration import machine_review_gap

from tests.test_auto_verdict import _CLEAN_REVIEW, _events, _submitted_task

# _submitted_task pair-starts as this agent, so a token under the same name is
# the implementer; anything else is an independent reviewer.
IMPLEMENTER = "dev-agent"


def _auth(monkeypatch) -> dict[str, dict[str, str]]:
    """Turn authentication on AFTER the fixture task is built.

    The shared setup calls the API without a token, so switching auth on
    earlier would fail the arrangement rather than the thing under test.
    """
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "implementer-token": TokenIdentity(IMPLEMENTER, "agent", principal_id=7),
            "reviewer-token": TokenIdentity("reviewer-bot", "agent", principal_id=9),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        "implementer": {"Authorization": "Bearer implementer-token"},
        "reviewer": {"Authorization": "Bearer reviewer-token"},
    }


async def _post_review(
    client: AsyncClient, task_id: int, headers: dict[str, str], **overrides
) -> dict:
    body = dict(_CLEAN_REVIEW)
    body.update(overrides)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=body, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_implementer_machine_review_is_guarded(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """AC-1: the report is stored, and stored as what it is.

    Refusing outright would take machine review away as a queue mechanism,
    which the statement rules out. So the report lands — carrying the fact
    that its author reviewed themselves, where an auditor will meet it.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, "self-guard", None)
    auth = _auth(monkeypatch)

    stored = await _post_review(client, task_id, auth["implementer"])

    assert stored["self_reviewed"] is True
    # The brief is where the next reviewer meets this report; the fact must
    # be waiting there, not only in the row the intake happened to return.
    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["reviewer"])
    ).json()
    assert brief["machine_review"]["self_reviewed"] is True


async def test_independent_review_is_not_flagged(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """The flag must mean something: an independent report carries none."""
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, "independent", None)
    auth = _auth(monkeypatch)

    stored = await _post_review(client, task_id, auth["reviewer"])

    assert stored["self_reviewed"] is False


async def test_submitted_by_cannot_disguise_a_self_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """The flag is taken from the token, not from the report's own claim.

    ``submitted_by`` is written as ``body.agent or identity.username`` — the
    caller names themselves. A guard that read it would be asking the checked
    party to describe itself, which is the failure #893 removed elsewhere.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, "disguise", None)
    auth = _auth(monkeypatch)

    stored = await _post_review(
        client, task_id, auth["implementer"], agent="totally-independent-bot"
    )

    assert stored["self_reviewed"] is True


async def test_self_review_does_not_satisfy_the_machine_review_requirement(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """The queue-passing use the statement quotes, closed at its own gate.

    machine_review_gap asks whether a review ran, and already refuses a report
    that shows no sign of having run. A report by the author is the same
    substitution one step over: present, and not the thing required.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, "queue-pass", None)
    auth = _auth(monkeypatch)
    await _post_review(client, task_id, auth["implementer"])

    row = await repo.get_task(db, task_id)
    gap = await machine_review_gap(db, dict(row))

    assert gap and "собственной работе" in gap


async def test_auto_verdict_refuses_a_self_reviewed_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """Grounds otherwise clean, and still no verdict — loudly.

    Without this the flag would be decorative: the policy would keep signing
    off self-reviews as independent. Escalation, not a silent refusal — the
    module reserves silence for missing data, and this is a positive finding.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _submitted_task(client, db, "auto-self", {"verdict": "auto"})
    auth = _auth(monkeypatch)

    await _post_review(client, task_id, auth["implementer"])

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth["reviewer"])).json()
    assert body["review_verdict"] != "approved"
    assert body["status"] == "review", "the task stays with the human gate"
    escalations = await _events(db, "verdict_escalated", task_id)
    assert escalations, "a self-reviewed report must not be refused in silence"


async def test_solo_mode_records_the_self_approval(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """Solo mode permits it — and the audit trail says that is what happened.

    Same lever, same meaning as on the human path (#434): the verdict is
    recorded as self-approved rather than passed off as independent.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    task_id = await _submitted_task(client, db, "solo-self", {"verdict": "auto"})
    auth = _auth(monkeypatch)

    await _post_review(client, task_id, auth["implementer"])

    body = (await client.get(f"/api/tasks/{task_id}", headers=auth["reviewer"])).json()
    assert body["review_verdict"] == "approved"
    row = await (
        await db.execute(
            "SELECT review_self_approved FROM tasks WHERE id = ?", (task_id,)
        )
    ).fetchone()
    assert row["review_self_approved"], "solo self-approval must be auditable"
