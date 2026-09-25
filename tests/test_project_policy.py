"""Признак теневого участия стюарда на проекте default (#1268).

Замок #743 запрещает отдавать гейты хаба машине. Теневое участие ничего не
отдаёт: стюард судит, вердикт выносит человек. Поэтому признак принимается и
на default, а делегирующие значения рядом с ним — по-прежнему нет.
"""

from __future__ import annotations

import json

from httpx import AsyncClient

from hub import repository as repo


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
    # null здесь больше не «неверный тип», а удаление ключа (#1427).
    for bad in ("true", 1, "yes", {}):
        resp = await client.patch(
            f"/api/projects/{pid}", json={"gate_policy": {"steward_shadow": bad}}
        )
        assert resp.status_code == 422, f"steward_shadow={bad!r}: {resp.text}"
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"steward_shadow": False}}
    )
    assert resp.status_code == 200, resp.text


# --- #1427: PATCH сливает gate_policy по ключам, а не заменяет целиком ----

#: Политика проекта default на проде 25.09 — образец из постановки #1427.
_DEFAULT_POLICY = {
    "dor": "human",
    "verdict": "human",
    "review": "dispatch",
    "release": "auto",
    "steward_shadow": True,
    "review_limit": 8,
    "review_limit_mode": "warn",
}


async def _policy_events(db, pid: int) -> list[dict]:
    cur = await db.execute(
        "SELECT payload FROM events WHERE kind = ? AND project_id = ? ORDER BY id",
        ("project_gate_policy_changed", pid),
    )
    return [json.loads(r[0]) for r in await cur.fetchall()]


async def test_patch_merges_gate_policy_keeping_omitted_keys(client: AsyncClient):
    """#1427 AC-1: правка одного ключа не стирает остальные."""
    pid = await _create_project(client, "default")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": _DEFAULT_POLICY}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"deep_daily_cap": 4}}
    )
    assert resp.status_code == 200, resp.text
    expected = {**_DEFAULT_POLICY, "deep_daily_cap": 4}
    assert resp.json()["gate_policy"] == expected

    listed = {p["slug"]: p for p in (await client.get("/api/projects")).json()}
    assert listed["default"]["gate_policy"] == expected


async def test_patch_null_removes_one_gate_policy_key(client: AsyncClient, db):
    """#1427 AC-2: null у ключа удаляет его, остальные на месте, аудит называет."""
    pid = await _create_project(client, "default")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": _DEFAULT_POLICY}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"review_limit": None}}
    )
    assert resp.status_code == 200, resp.text
    expected = {k: v for k, v in _DEFAULT_POLICY.items() if k != "review_limit"}
    assert resp.json()["gate_policy"] == expected

    events = await _policy_events(db, pid)
    assert events, "изменение политики оставляет событие аудита"
    assert events[-1]["removed"] == ["review_limit"]
    assert events[-1]["changed"] == []


async def test_patch_lock_checks_run_on_merged_policy(client: AsyncClient, db):
    """#1427 AC-3: замок #743 смотрит на итоговую политику, а не на кусок.

    Делегирующее значение, уже лежащее в строке default (положено мимо API),
    не проходит через PATCH, который трогает только другой ключ: иначе такой
    PATCH записал бы запрещённую политику заново, как одобренную.
    """
    pid = await _create_project(client, "default")
    stored = {**_DEFAULT_POLICY, "verdict": "steward"}
    await repo.update_project(db, pid, gate_policy=json.dumps(stored))
    await db.commit()

    resp = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"deep_daily_cap": 4}}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "default_project_gate_locked"

    row = await repo.get_project(db, pid)
    assert json.loads(row["gate_policy"]) == stored, "отказ ничего не записал"
    assert await _policy_events(db, pid) == []

    # Тот же PATCH, снимающий делегата, проходит: итоговая политика чиста.
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"deep_daily_cap": 4, "verdict": None}},
    )
    assert resp.status_code == 200, resp.text
    assert "verdict" not in resp.json()["gate_policy"]
