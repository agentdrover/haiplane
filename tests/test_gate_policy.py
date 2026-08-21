"""Per-project gate policy: storage, human-only writes, default lock (#743).

Shadow step of feature #738: the policy is stored, validated and visible,
and deliberately decides NOTHING until #744 starts reading it. The default
project — the hub's own repo — refuses any 'auto' from any token: the hub
does not weaken oversight over itself.
"""

from __future__ import annotations

from httpx import AsyncClient

from hub import config
from hub.config import TokenIdentity


async def _create_project(client: AsyncClient, slug: str, **headers) -> int:
    resp = await client.post(
        "/api/projects", json={"slug": slug, "name": slug.title()}, **headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_policy_stored_and_returned(client: AsyncClient):
    # AC-1 (#743): a set policy is visible in the API; absence reads as {}.
    pid = await _create_project(client, "spike-a")
    plain = await _create_project(client, "spike-b")

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"dor": "auto"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == {"dor": "auto"}

    listed = {p["slug"]: p for p in (await client.get("/api/projects")).json()}
    assert listed["spike-a"]["gate_policy"] == {"dor": "auto"}
    assert listed["spike-b"]["gate_policy"] == {}, (
        "no policy means {} — every gate human by default"
    )
    assert plain == listed["spike-b"]["id"]


async def test_policy_shape_is_validated(client: AsyncClient):
    # Unknown keys and values are mistakes worth refusing, not ignoring.
    pid = await _create_project(client, "spike-shape")
    for bad in (
        {"dor": "yolo"},
        {"unknown": "auto"},
        {"decision": "auto"},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": bad})
        assert resp.status_code == 422, f"{bad} must be refused: {resp.text}"
    body = (await client.get("/api/projects")).json()
    row = next(p for p in body if p["id"] == pid)
    assert row["gate_policy"] == {}, "a refused write must leave nothing behind"


async def test_agent_token_cannot_set_policy(client: AsyncClient, monkeypatch):
    # AC-2 (#743): the write rides the human-only project PATCH — an agent
    # token gets a structured 403 and the policy stays untouched.
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"headers": {"Authorization": "Bearer human-token"}}
    agent = {"headers": {"Authorization": "Bearer agent-token"}}

    pid = await _create_project(client, "spike-guard", **human)

    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto"}},
        **agent,
    )
    assert resp.status_code == 403

    listed = (await client.get("/api/projects", **human)).json()
    row = next(p for p in listed if p["id"] == pid)
    assert row["gate_policy"] == {}


async def test_default_project_locked(client: AsyncClient):
    # AC-3 (#743): the hub's own project refuses 'auto' at any gate from
    # any token — the system does not simplify its own rules.
    pid = await _create_project(client, "default")

    for payload in (
        {"dor": "auto"},
        {"verdict": "auto"},
        {"dor": "auto", "verdict": "human"},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": payload})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "default_project_gate_locked"

    # An explicit all-human policy is fine — it changes nothing.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "human", "verdict": "human"}},
    )
    assert resp.status_code == 200, resp.text


async def test_policy_is_inert_in_this_task(client: AsyncClient, monkeypatch):
    # AC-4 (#743): nothing reads the policy yet. With the global switch off
    # (today's default), a DoR-passed R0 draft stays waiting for the human
    # even though a project with full auto policy exists — the policy alone
    # activates nothing until #744.
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    pid = await _create_project(client, "spike-inert")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto", "verdict": "auto"}},
    )
    assert resp.status_code == 200, resp.text

    draft = await client.post(
        "/api/tasks", json={"title": "inert probe", "source": "agent"}
    )
    task_id = draft.json()["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["module"],
            "validation_commands": ["uv run pytest -q"],
            "size": "S",
            "wip_tag": "feature_work",
            "affected_areas": ["docs/notes.md"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["dor_passed"] is True
    assert body["risk_class"] == "R0"
    assert body["status"] == "draft", "the policy must decide nothing until #744"


async def test_risk_map_and_ceiling_are_human_only(client: AsyncClient, monkeypatch):
    """#760 AC-4: the new knobs ride the same human-only PATCH as the gates.

    They decide how far the DoR autopilot reaches, so an agent able to write
    them could widen its own gate — the conflict #743 removed for dor/verdict.
    """
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"headers": {"Authorization": "Bearer human-token"}}
    agent = {"headers": {"Authorization": "Bearer agent-token"}}

    pid = await _create_project(client, "spike-knobs", **human)
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"dor": "auto", "risk_map": {"src/**": "code"}}},
        **agent,
    )
    assert resp.status_code == 403
    listed = (await client.get("/api/projects", **human)).json()
    assert next(p for p in listed if p["id"] == pid)["gate_policy"] == {}

    ok = await client.patch(
        f"/api/projects/{pid}",
        json={
            "gate_policy": {
                "dor": "auto",
                "dor_max_class": "r1",
                "risk_map": {"src/**": "code"},
            }
        },
        **human,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["gate_policy"]["risk_map"] == {"src/**": "code"}


async def test_risk_map_and_ceiling_refuse_nonsense(client: AsyncClient):
    """#760: a malformed knob is refused loudly, never stored half-understood."""
    pid = await _create_project(client, "spike-shapes")
    for payload in (
        {"risk_map": {"src/**": "whatever"}},
        {"risk_map": {"": "code"}},
        {"risk_map": ["src/**"]},
        {"dor_max_class": "r3"},
        {"dor_max_class": "R1 "},
    ):
        resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": payload})
        assert resp.status_code == 422, f"{payload} must be refused: {resp.text}"

    listed = (await client.get("/api/projects")).json()
    assert next(p for p in listed if p["id"] == pid)["gate_policy"] == {}, (
        "a refused write must leave nothing behind"
    )


# --- The review key (#805) ---------------------------------------------------


async def test_review_key_is_stored_and_validated(client: AsyncClient):
    # The key is part of the policy shape, so a typo is refused at the door
    # rather than stored as a knob nothing reads. (The dispatcher ALSO reads
    # an unknown value as off — a policy written straight into the DB must
    # not spend tokens either.)
    pid = await _create_project(client, "spike-review")

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispatch"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == {"review": "dispatch"}

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review": "dispath"}}
    )
    assert resp.status_code == 422, resp.text


async def test_default_project_may_ask_for_review(client: AsyncClient):
    # The #743 lock is about handing the hub's own gates to the autopilot.
    # Calling a reviewer does the opposite: the human keeps the gate and
    # finally has something to read at it (#804). Refusing this would have
    # meant the hub's own code is the one code nobody reviews.
    pid = await _create_project(client, "default")

    resp = await client.patch(
        f"/api/projects/{pid}",
        json={
            "gate_policy": {"dor": "human", "verdict": "human", "review": "dispatch"}
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"]["review"] == "dispatch"

    # The lock itself is untouched.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"verdict": "auto", "review": "dispatch"}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "default_project_gate_locked"
