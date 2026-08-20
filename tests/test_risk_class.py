"""Risk class derivation (#582): facts in, class + reasons out.

The class is computed from declared ``affected_areas`` only. Nothing the
author says about risk — in create or refine payloads — may influence it,
any migration is at least R3 (owner's rule of 2026-07-31), and every class
arrives with the reasons that produced it.
"""

from __future__ import annotations

from httpx import AsyncClient

from hub.models import RiskClass
from hub.services.risk_class import derive_risk_class

# ---------------------------------------------------------------------------
# Unit level: the pure derivation
# ---------------------------------------------------------------------------


def test_no_declared_areas_means_not_computed():
    risk, reasons = derive_risk_class([])
    assert risk is None
    assert reasons == []
    risk, reasons = derive_risk_class(None)
    assert risk is None
    assert reasons == []


def test_unknown_paths_cost_more_than_docs_not_less():
    # CI configs and deploy scripts live outside the known map; unknown must
    # never be cheaper than known-benign.
    risk, reasons = derive_risk_class([".github/workflows/ci.yml"])
    assert risk is RiskClass.r2
    assert any(".github/workflows/ci.yml" in r for r in reasons)


def test_harmless_path_cannot_lower_a_risky_class():
    risky, _ = derive_risk_class(["hub/db.py"])
    mixed, _ = derive_risk_class(["hub/db.py", "docs/notes.md"])
    assert mixed is risky is RiskClass.r3


# ---------------------------------------------------------------------------
# AC-1: docs-only change is a low class, with its reasons listed
# ---------------------------------------------------------------------------


async def _create_task(client: AsyncClient, **overrides) -> dict:
    body = {"title": "risk probe", **overrides}
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_docs_only_change_is_low_class(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"affected_areas": ["docs/agent-context/invariants.md"]},
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["risk_class"] == "R0"
    assert body["risk_class_reasons"], "a bare class without reasons is unarguable"
    assert any("документац" in r for r in body["risk_class_reasons"])


# ---------------------------------------------------------------------------
# AC-2: any migration is at least R3, regardless of everything else
# ---------------------------------------------------------------------------


async def test_any_migration_is_at_least_r3(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"affected_areas": ["hub/db.py", "docs/readme.md", "tests/test_db.py"]},
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["risk_class"] in {"R3", "R4", "R5"}
    assert any("миграци" in r for r in body["risk_class_reasons"])


# ---------------------------------------------------------------------------
# AC-3: auth / role checks are at least R3
# ---------------------------------------------------------------------------


async def test_auth_change_is_at_least_r3(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"affected_areas": ["hub/auth.py"]},
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["risk_class"] in {"R3", "R4", "R5"}
    assert any("hub/auth.py" in r for r in body["risk_class_reasons"])


# ---------------------------------------------------------------------------
# AC-4: a self-declared class never influences the result
# ---------------------------------------------------------------------------


async def test_self_declared_class_is_ignored(client: AsyncClient):
    # Declared in refine: the docs-only facts say R0; the author says R5.
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"affected_areas": ["docs/a.md"], "risk_class": "R5"},
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["risk_class"] == "R0"

    # Declared at creation, the other direction: facts say R3 (migration);
    # the author claims R0.
    created = await _create_task(
        client,
        affected_areas=["hub/db.py"],
        risk_class="R0",
    )
    body = (await client.get(f"/api/tasks/{created['id']}")).json()
    assert body["risk_class"] == "R3"


# ---------------------------------------------------------------------------
# AC-5: the reasons behind the class are readable, not just the letter
# ---------------------------------------------------------------------------


async def test_class_exposes_its_reasons(client: AsyncClient):
    created = await _create_task(
        client,
        affected_areas=["hub/db.py", "hub/models.py", "hub/templates/a.html"],
    )
    body = (await client.get(f"/api/tasks/{created['id']}")).json()
    assert body["risk_class"] == "R3"
    reasons = body["risk_class_reasons"]
    # Every triggered feature is named: migration floor, contract change,
    # presentation — three features, three reasons.
    assert len(reasons) == 3
    assert any("hub/db.py" in r for r in reasons)
    assert any("hub/models.py" in r for r in reasons)
    assert any("hub/templates/a.html" in r for r in reasons)


async def test_clearing_areas_returns_class_to_not_computed(client: AsyncClient):
    created = await _create_task(client, affected_areas=["hub/db.py"])
    assert (await client.get(f"/api/tasks/{created['id']}")).json()[
        "risk_class"
    ] == "R3"
    resp = await client.post(
        f"/api/tasks/{created['id']}/refine", json={"affected_areas": []}
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{created['id']}")).json()
    assert body["risk_class"] is None, "no facts left — the class must not linger"
    assert body["risk_class_reasons"] == []
