"""Risk class derivation (#582): facts in, class + reasons out.

The class is computed from declared ``affected_areas`` only. Nothing the
author says about risk — in create or refine payloads — may influence it,
any migration is at least R3 (owner's rule of 2026-07-31), and every class
arrives with the reasons that produced it.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import RiskClass, TaskCreate
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


# ---------------------------------------------------------------------------
# #583: recompute from the ACTUAL diff at submission, escalate upward only
# ---------------------------------------------------------------------------


class _DiffGitOps(NoopGitOps):
    """Stands in for the branch diff; None means "could not be determined"."""

    def __init__(self, paths: list[str] | None) -> None:
        self._paths = paths

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths


async def _running_task(db: aiosqlite.Connection, areas: list[str]) -> int:
    tv = await services.create_task(
        db, TaskCreate(title="risk recompute", affected_areas=areas)
    )
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()
    started = await services.pair_start_task(db, tv.id, caller="dev-agent")
    assert started.status.value == "running"
    return tv.id


async def _updates(db: aiosqlite.Connection, task_id: int, kind: str) -> list[str]:
    rows = await repo.get_task_updates(db, task_id)
    return [u["content"] for u in rows if u["kind"] == kind]


async def test_diff_with_migration_escalates_running_task(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1 (#583): declared as web-only work (R2), the diff wandered into
    # hub/db.py — the class climbs to R3 and the divergence is put in front
    # of the owner rather than waved through.
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task(db, ["hub/web.py"])
    assert dict(await repo.get_task(db, task_id))["risk_class"] == "R2"
    plugins.git_ops = _DiffGitOps(["hub/web.py", "hub/db.py"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review", (
        "escalation must leave a legal path forward, not lock the task"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["risk_class"] == "R3"
    alerts = [a for a in await _updates(db, task_id, "alert") if "Класс риска" in a]
    assert alerts, "an upward divergence must be visible, not silent"


async def test_class_does_not_auto_downgrade(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2 (#583): the diff turned out narrower than the spec — doing less
    # than promised is not grounds for dropping oversight.
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task(db, ["hub/db.py"])
    assert dict(await repo.get_task(db, task_id))["risk_class"] == "R3"
    plugins.git_ops = _DiffGitOps(["docs/notes.md"])

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    row = dict(await repo.get_task(db, task_id))
    assert row["risk_class"] == "R3", "the class must never drop automatically"
    assert not [
        a for a in await _updates(db, task_id, "alert") if "Класс риска" in a
    ], "keeping the class is not an escalation and must not raise alerts"


async def test_escalation_records_both_classes_and_reason(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3 (#583): the feed shows the original class, the recomputed one and
    # the feature that caused the raise — «R3, потому что миграция» can be
    # argued with, a bare «R3» cannot.
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task(db, ["hub/web.py"])
    plugins.git_ops = _DiffGitOps(["hub/db.py"])

    await services.submit_for_review(db, task_id)

    alerts = [a for a in await _updates(db, task_id, "alert") if "Класс риска" in a]
    assert len(alerts) == 1
    assert "R2" in alerts[0] and "R3" in alerts[0]
    assert "hub/db.py" in alerts[0]


async def test_unresolvable_diff_degrades_instead_of_failing(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-4 (#583): nothing to resolve the diff with — the submission still
    # goes through, the class stays put, and the "not recomputed" state is
    # written where the reviewer will read it.
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _running_task(db, ["hub/web.py"])
    plugins.git_ops = _DiffGitOps(None)

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review", "the submit path must not fail"
    row = dict(await repo.get_task(db, task_id))
    assert row["risk_class"] == "R2", "an unreadable diff must not touch the class"
    statuses = await _updates(db, task_id, "status")
    assert any("НЕ пересчитан" in s for s in statuses), (
        "a skipped recompute must be visible, not an absence of news"
    )
