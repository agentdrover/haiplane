"""Признак теневого участия стюарда на проекте default (#1268).

Замок #743 запрещает отдавать гейты хаба машине. Теневое участие ничего не
отдаёт: стюард судит, вердикт выносит человек. Поэтому признак принимается и
на default, а делегирующие значения рядом с ним — по-прежнему нет.
"""

from __future__ import annotations

from httpx import AsyncClient


async def _create_project(client: AsyncClient, slug: str) -> int:
    resp = await client.post("/api/projects", json={"slug": slug, "name": slug.title()})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_default_accepts_shadow_participation_but_not_delegation(
    client: AsyncClient,
):
    """#1268 AC-2: default сохраняет тень при verdict=human и отказывает делегированию."""
    pid = await _create_project(client, "default")
    shadow = {
        "dor": "human",
        "verdict": "human",
        "review": "dispatch",
        "release": "auto",
        "steward_shadow": True,
    }

    resp = await client.patch(f"/api/projects/{pid}", json={"gate_policy": shadow})
    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_policy"] == shadow

    listed = {p["slug"]: p for p in (await client.get("/api/projects")).json()}
    assert listed["default"]["gate_policy"]["steward_shadow"] is True

    for gate in ("verdict", "dor"):
        resp = await client.patch(
            f"/api/projects/{pid}",
            json={"gate_policy": {**shadow, gate: "steward"}},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "default_project_gate_locked"

    listed = {p["slug"]: p for p in (await client.get("/api/projects")).json()}
    assert listed["default"]["gate_policy"] == shadow, (
        "отказ замка не оставляет следа в сохранённой политике"
    )


async def test_the_shadow_flag_accepts_only_a_boolean(client: AsyncClient):
    """Запись признака неверного типа отказывается громко, а не сохраняется."""
    pid = await _create_project(client, "spike-shadow-shape")
    for bad in ("true", 1, "yes", None, {}):
        resp = await client.patch(
            f"/api/projects/{pid}", json={"gate_policy": {"steward_shadow": bad}}
        )
        assert resp.status_code == 422, f"steward_shadow={bad!r}: {resp.text}"
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"steward_shadow": False}}
    )
    assert resp.status_code == 200, resp.text
