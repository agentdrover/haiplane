"""Auto-approval of low-risk drafts (#584): narrow band, switch, audit.

R0/R1 drafts that passed DoR may skip the human approval — but only under
an explicit switch, only with a computed class, never for gate/ladder
changes, and always with the reason written into the feed as the hub.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo

_DOR_READY = {
    "work_type": "feature",
    "user_story": "as a user, I want X so that Y",
    "problem_statement": "ps",
    "business_value": "bv",
    "scope_in": ["module"],
    "validation_commands": ["uv run pytest -q"],
    "size": "S",
    "wip_tag": "feature_work",
    "acceptance_criteria": [
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
        }
    ],
}


async def _draft(client: AsyncClient, title: str = "auto approve probe") -> int:
    resp = await client.post("/api/tasks", json={"title": title, "source": "agent"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    return body["id"]


async def _refine_to_dor(client: AsyncClient, task_id: int, areas: list[str]) -> dict:
    payload = dict(_DOR_READY)
    payload["affected_areas"] = areas
    resp = await client.post(f"/api/tasks/{task_id}/refine", json=payload)
    assert resp.status_code == 200, resp.text
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def test_low_class_draft_is_auto_approved_with_reason(
    client: AsyncClient, monkeypatch
) -> None:
    # AC-1 (#584): an R0 draft that passes DoR under the switch is approved
    # without a human, and the feed names the class and its features.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["risk_class"] == "R0"
    assert body["status"] == "open", "the human gate must not be waited on"
    feed = [u["content"] for u in body["updates"] or []]
    auto = [c for c in feed if "Автоодобрено" in c]
    assert auto, "an auto-approval without a recorded reason is unanswerable"
    assert "R0" in auto[0]
    assert "документац" in auto[0], "the features, not just the letter"


async def test_switch_off_restores_human_gate(client: AsyncClient, monkeypatch) -> None:
    # AC-2 (#584): with the switch off the behavior is exactly today's.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["risk_class"] == "R0"
    assert body["dor_passed"] is True
    assert body["status"] == "draft", "off must mean the human approves"


async def test_r3_and_above_still_require_human(
    client: AsyncClient, monkeypatch
) -> None:
    # AC-3 (#584): the band is narrow — R3 waits for the owner.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["hub/db.py"])

    assert body["risk_class"] == "R3"
    assert body["status"] == "draft"


async def test_r2_is_not_in_the_band_yet(client: AsyncClient, monkeypatch) -> None:
    # #585 opens R2 only after measured reviewer agreement; today the switch
    # itself refuses to name it.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r2")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["status"] == "draft", (
        "an unknown/unsupported switch value must fail toward the human gate"
    )


async def test_unclassified_draft_is_never_auto_approved(
    client: AsyncClient, monkeypatch
) -> None:
    # AC-4 (#584): absence of a class is not low risk.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _draft(client)
    payload = dict(_DOR_READY)  # no affected_areas → class stays not computed
    resp = await client.post(f"/api/tasks/{task_id}/refine", json=payload)
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task_id}")).json()

    assert body["risk_class"] is None
    assert body["dor_passed"] is True
    assert body["status"] == "draft"


async def test_gate_changes_are_never_auto_approved(
    client: AsyncClient, monkeypatch
) -> None:
    # AC-5 (#584): the system does not simplify its own rules — the process
    # docs that DEFINE the gates are R0 by path, and still wait for the owner.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["docs/agent-context/invariants.md"])

    assert body["risk_class"] == "R0"
    assert body["status"] == "draft", "ladder surfaces stay with the owner"


async def test_auto_approval_is_recorded_as_hub(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-6 (#584): the record must not look like a human's decision.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["tests/test_notes.py"])
    assert body["risk_class"] == "R1"
    assert body["status"] == "open"

    rows = await repo.get_task_updates(db, task_id)
    auto = [dict(u) for u in rows if "Автоодобрено" in u["content"]]
    assert len(auto) == 1
    assert auto[0]["author_kind"] == "hub"
    assert auto[0]["principal_id"] is None


async def test_r1_band_respects_the_r0_ceiling(
    client: AsyncClient, monkeypatch
) -> None:
    # The ceiling is a ceiling: under r0, an R1 draft still waits.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r0")
    task_id = await _draft(client)
    body = await _refine_to_dor(client, task_id, ["tests/test_notes.py"])

    assert body["risk_class"] == "R1"
    assert body["status"] == "draft"
