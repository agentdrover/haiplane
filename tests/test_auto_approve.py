"""Auto-approval of low-risk drafts (#584) scoped per project (#744).

The band (R0/R1) says WHAT is safe, the project's gate_policy says WHERE
automation is allowed, and the global switch stays the kill-switch and the
class ceiling. Every failure mode — no project, no policy, unparsable
policy — refuses toward the human gate, never toward auto.
"""

from __future__ import annotations

import json

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


async def _project(db: aiosqlite.Connection, slug: str, policy: dict | None) -> int:
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


async def _node(
    db: aiosqlite.Connection,
    *,
    title: str,
    task_type: str,
    parent_id: int | None,
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


async def _draft_in_project(
    client: AsyncClient, db: aiosqlite.Connection, project_id: int | None
) -> int:
    """A draft task under epic→feature, the epic bound to the project."""
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    if project_id is not None:
        await repo.update_task(db, epic, project_id=project_id)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    await db.commit()
    resp = await client.post(
        "/api/tasks",
        json={
            "title": "auto approve probe",
            "source": "agent",
            "task_type": "task",
            "parent_id": feature,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    return body["id"]


async def _refine_to_dor(
    client: AsyncClient, task_id: int, areas: list[str] | None
) -> dict:
    payload = dict(_DOR_READY)
    if areas is not None:
        payload["affected_areas"] = areas
    resp = await client.post(f"/api/tasks/{task_id}/refine", json=payload)
    assert resp.status_code == 200, resp.text
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def _approved_events(db: aiosqlite.Connection) -> list[dict]:
    rows = await repo.list_events(db, since=0, kinds=["task_approved"], limit=200)
    return [dict(r) for r in rows]


async def test_low_class_draft_is_auto_approved_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #584 AC-1 + #744: an R0 draft in a project with dor=auto is approved
    # without a human, and the feed names the project, class and features.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["risk_class"] == "R0"
    assert body["status"] == "open", "the human gate must not be waited on"
    feed = [u["content"] for u in body["updates"] or []]
    auto = [c for c in feed if "Автоодобрено" in c]
    assert auto, "an auto-approval without a recorded reason is unanswerable"
    assert "R0" in auto[0]
    assert "spike" in auto[0], "the project whose policy decided must be named"
    assert "документац" in auto[0], "the features, not just the letter"


async def test_dor_autopilot_is_project_scoped(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #744 AC-1: same draft, same class, same switch — the only difference
    # is the project's policy, and only that project auto-approves.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    auto_pid = await _project(db, "spike-auto", {"dor": "auto"})
    plain_pid = await _project(db, "spike-plain", None)

    auto_task = await _draft_in_project(client, db, auto_pid)
    plain_task = await _draft_in_project(client, db, plain_pid)

    auto_body = await _refine_to_dor(client, auto_task, ["docs/notes.md"])
    plain_body = await _refine_to_dor(client, plain_task, ["docs/notes.md"])

    assert auto_body["status"] == "open"
    assert plain_body["status"] == "draft", "no project policy — the human gate stands"


async def test_kill_switch_beats_project_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #744 AC-2 (and #584 AC-2): the global switch off restores the human
    # gate everywhere, whatever any project's policy says.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    pid = await _project(db, "spike-killed", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["risk_class"] == "R0"
    assert body["dor_passed"] is True
    assert body["status"] == "draft", "off must mean the human approves"


async def test_switch_off_restores_human_gate(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # Kept under its #584 AC-2 name: identical contract to the kill-switch
    # test above — off is today's behavior in full.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    pid = await _project(db, "spike-off", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])
    assert body["status"] == "draft"


async def test_policy_autoapproval_actor_is_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #744 AC-3: the event says a POLICY decided — machine-distinguishable
    # from a human click and from other hub service writes, which is what
    # lets the human_gates metric (#737) keep the autopilot out of both
    # columns.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-actor", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["tests/test_x.py"])
    assert body["status"] == "open"

    events = [e for e in await _approved_events(db) if e["task_id"] == task_id]
    assert len(events) == 1
    assert events[0]["actor"] == "policy"
    payload = json.loads(events[0]["payload"])
    assert payload["project"] == "spike-actor"
    assert payload["risk_class"] == "R1"


async def test_ladder_stoplist_survives_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #744 AC-4 (and #584 AC-5): gate/ladder surfaces wait for the owner at
    # any class, in any project — no policy can switch that off.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-ladder", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/agent-context/invariants.md"])

    assert body["risk_class"] == "R0"
    assert body["status"] == "draft", "ladder surfaces stay with the owner"


async def test_gate_changes_are_never_auto_approved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #584 AC-5 name kept: same contract as the ladder test above.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-gates", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["hub/config.py"])
    assert body["status"] == "draft"


async def test_r3_and_above_still_require_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #584 AC-3: the band is narrow — R3 waits for the owner.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-r3", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["hub/db.py"])

    assert body["risk_class"] == "R3"
    assert body["status"] == "draft"


async def test_r2_is_not_in_the_band_yet(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #585 opens R2 only after measured reviewer agreement; the switch
    # itself refuses to name it.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r2")
    pid = await _project(db, "spike-r2", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["status"] == "draft", (
        "an unknown/unsupported switch value must fail toward the human gate"
    )


async def test_unclassified_draft_is_never_auto_approved(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #584 AC-4: absence of a class is not low risk.
    #
    # Since #842 a code-typed task cannot reach DoR without declaring areas,
    # so the only way to be READY and still unclassified is a work type that
    # legitimately has nothing to declare — docs. The rule under test is
    # unchanged: no class means no auto-approval, whatever the ceiling says.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-null", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    docs_payload = dict(_DOR_READY, work_type="docs")
    resp = await client.post(f"/api/tasks/{task_id}/refine", json=docs_payload)
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["dor_passed"] is True, "docs work is ready without areas"

    assert body["risk_class"] is None
    assert body["dor_passed"] is True
    assert body["status"] == "draft"


async def test_auto_approval_is_recorded_as_hub(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # #584 AC-6: the FEED record must not look like a human's decision —
    # hub authorship, no principal. (The EVENT actor is 'policy', asserted
    # separately above.)
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-author", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["tests/test_notes.py"])
    assert body["risk_class"] == "R1"
    assert body["status"] == "open"

    rows = await repo.get_task_updates(db, task_id)
    auto = [dict(u) for u in rows if "Автоодобрено" in u["content"]]
    assert len(auto) == 1
    assert auto[0]["author_kind"] == "hub"
    assert auto[0]["principal_id"] is None


async def test_r1_band_respects_the_r0_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # The ceiling is a ceiling: under r0, an R1 draft still waits — and a
    # project policy cannot raise it.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r0")
    pid = await _project(db, "spike-ceiling", {"dor": "auto"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["tests/test_notes.py"])

    assert body["risk_class"] == "R1"
    assert body["status"] == "draft"


async def test_unbound_task_refuses_toward_human(
    client: AsyncClient, monkeypatch
) -> None:
    # A draft whose project cannot be resolved (no default project row in
    # this DB) must refuse toward the human gate — never default to auto.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    resp = await client.post(
        "/api/tasks", json={"title": "unbound probe", "source": "agent"}
    )
    task_id = resp.json()["id"]
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    assert body["risk_class"] == "R0"
    assert body["dor_passed"] is True
    assert body["status"] == "draft"


async def test_project_ceiling_only_tightens_the_global_one(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """#760 AC-3: a project may be stricter than the switch, never looser.

    The env is the kill-switch and the upper bound. A project asking for more
    than it allows gets the env's answer — otherwise "who opened this gate"
    would have two possible answers and no way to tell them apart.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    strict_pid = await _project(
        db, "spike-strict", {"dor": "auto", "dor_max_class": "r0"}
    )
    task_id = await _draft_in_project(client, db, strict_pid)
    body = await _refine_to_dor(client, task_id, ["tests/test_probe.py"])

    assert body["risk_class"] == "R1"
    assert body["status"] == "draft", (
        "R1 is inside the global band but above the project's own ceiling"
    )

    open_pid = await _project(db, "spike-open", {"dor": "auto", "dor_max_class": "r1"})
    other = await _draft_in_project(client, db, open_pid)
    other_body = await _refine_to_dor(client, other, ["tests/test_probe.py"])
    assert other_body["status"] == "open", (
        "the same class passes where the project allows it"
    )


async def test_auto_approval_names_both_ceilings(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """#760 AC-5: the feed line says which lever decided."""
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(db, "spike-both", {"dor": "auto", "dor_max_class": "r1"})
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(client, task_id, ["docs/notes.md"])

    auto = [
        c for c in (u["content"] for u in body["updates"] or []) if "Автоодобрено" in c
    ]
    assert auto, "an auto-approval without a recorded reason is unanswerable"
    assert "проектный R1" in auto[0], "the project ceiling must be named"
    assert "глобальный R1" in auto[0], "and the global one beside it"
    assert "HAIPLANE_AUTO_APPROVE_MAX_CLASS=r1" in auto[0]
    assert "OPENCLAW_AUTO_APPROVE_MAX_CLASS" in auto[0], (
        "the legacy name must stay documented during the soak"
    )


async def test_project_risk_map_lets_a_satellite_draft_pass(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """#760 AC-1 end to end: described paths stop costing R2 by default.

    The task that started this (#759) declared ``.env.example`` beside its
    docs and tests. Nothing in that list is risky, yet the dotfile is unknown
    to the built-in map and drags the whole draft to R2 — so the draft waits
    for a human it did not need. One rule describing it is the whole fix.
    """
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    unmapped_pid = await _project(db, "spike-unmapped", {"dor": "auto"})
    baseline = await _draft_in_project(client, db, unmapped_pid)
    baseline_body = await _refine_to_dor(
        client, baseline, [".env.example", "specs/quickstart.md", "tests/unit"]
    )
    assert baseline_body["risk_class"] == "R2", "today: one dotfile makes it R2"
    assert baseline_body["status"] == "draft"

    pid = await _project(
        db, "spike-mapped", {"dor": "auto", "risk_map": {".env*": "docs"}}
    )
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(
        client, task_id, [".env.example", "specs/quickstart.md", "tests/unit"]
    )

    assert body["risk_class"] == "R1", "docs + tests, nothing unknown left"
    assert body["status"] == "open"
    reasons = " ".join(body["risk_class_reasons"] or [])
    assert "вне известной карты" not in reasons
    assert "картой проекта" in reasons, "the owner's rule must be visible in the reason"


async def test_unmapped_path_still_costs_r2(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    """#760 AC-1 boundary: the map describes, it does not excuse."""
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await _project(
        db, "spike-partial", {"dor": "auto", "risk_map": {"specs/**": "docs"}}
    )
    task_id = await _draft_in_project(client, db, pid)
    body = await _refine_to_dor(
        client, task_id, ["specs/quickstart.md", "deploy/run.sh"]
    )

    assert body["risk_class"] == "R2"
    assert body["status"] == "draft", "an undescribed path keeps the human gate"
