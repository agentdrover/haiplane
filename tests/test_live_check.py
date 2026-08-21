"""Evidence that someone watched the work behave in production (#813, #811).

Three defects on 21.08.2026 — #801, #802, #803 — passed an APPROVED review and
a green CI and were found only by running against prod. None was visible in a
diff. The process had nowhere to record that such a run happened, so a task
that was exercised and one nobody touched looked identical.

These tests pin the two things that make the record worth having: it names an
author the caller cannot forge, and it refuses to be a stamp.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.config import TokenIdentity


def _auth(monkeypatch) -> dict[str, dict[str, str]]:
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent", principal_id=7),
            "human-token": TokenIdentity("denis", "human", principal_id=1),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {
        "agent": {"Authorization": "Bearer agent-token"},
        "human": {"Authorization": "Bearer human-token"},
    }


# Fixture shas are deliberately NOT hex-shaped: CI runs detect-secrets over
# the tree, and a plausible-looking hex string trips it as a high-entropy
# secret. The column is free text, so nothing here needs to look like a commit.


async def _task(client: AsyncClient, auth: dict, title: str = "Shipped") -> int:
    resp = await client.post("/api/tasks", json={"title": title}, headers=auth["human"])
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ---- AC-1: the record is stored, and its author comes from the token ----


async def test_evidence_is_stored_with_its_author(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    resp = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "done",
            "probe": "GET /api/sessions с агентским токеном",
            "observation": "200, список из одной сессии, online=true",
            "sha": "deployed-sha-one",
            # A body that tries to name someone else must not be believed.
            "recorded_agent": "somebody-else",
            "recorded_by": 999,
        },
        headers=auth["agent"],
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()
    assert stored["recorded_agent"] == "bot"
    assert stored["recorded_by"] == 7
    assert stored["probe"].startswith("GET /api/sessions")
    assert stored["observation"].startswith("200")
    assert stored["sha"] == "deployed-sha-one"

    listed = await client.get(
        f"/api/tasks/{task_id}/live-checks", headers=auth["agent"]
    )
    assert [c["id"] for c in listed.json()] == [stored["id"]]

    # The observation is also visible where a human reads the task.
    task = (await client.get(f"/api/tasks/{task_id}", headers=auth["human"])).json()
    notes = [u["content"] for u in task["updates"] or []]
    assert any("Живая проверка" in n for n in notes)


# ---- AC-2: "done" costs two facts ----


async def test_done_requires_both_probe_and_observation(
    client: AsyncClient, monkeypatch
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    for payload in (
        {"outcome": "done", "probe": "открыл дашборд"},
        {"outcome": "done", "observation": "всё хорошо"},
        {"outcome": "done"},
    ):
        resp = await client.post(
            f"/api/tasks/{task_id}/live-check", json=payload, headers=auth["agent"]
        )
        assert resp.status_code == 422, payload
        assert "incomplete_evidence" in resp.text, (
            "'checked, all good' is the shape a stamp takes — the schema, not "
            "goodwill, has to refuse it"
        )

    stored = await client.get(
        f"/api/tasks/{task_id}/live-checks", headers=auth["agent"]
    )
    assert stored.json() == [], "a refused record writes nothing"


# ---- AC-3: "nothing to observe" is a claim, and claims carry reasons ----


async def test_not_applicable_needs_a_reason(client: AsyncClient, monkeypatch):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    bare = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={"outcome": "not_applicable"},
        headers=auth["agent"],
    )
    assert bare.status_code == 422
    assert "missing_reason" in bare.text

    explained = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "not_applicable",
            "reason": "правка только в тестах, наблюдаемой поверхности в проде нет",
        },
        headers=auth["agent"],
    )
    assert explained.status_code == 200, explained.text
    assert explained.json()["outcome"] == "not_applicable"
    assert "тестах" in explained.json()["reason"]


# ---- AC-4: evidence accumulates per sha instead of overwriting ----


async def test_evidence_accumulates_per_sha(client: AsyncClient, monkeypatch):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    for sha, seen in (("sha-one", "404 — ручки ещё нет"), ("sha-two", "200 — есть")):
        resp = await client.post(
            f"/api/tasks/{task_id}/live-check",
            json={
                "outcome": "done",
                "probe": "GET /api/tasks/1/live-checks",
                "observation": seen,
                "sha": sha,
            },
            headers=auth["agent"],
        )
        assert resp.status_code == 200, resp.text

    checks = (
        await client.get(f"/api/tasks/{task_id}/live-checks", headers=auth["agent"])
    ).json()
    assert [c["sha"] for c in checks] == ["sha-two", "sha-one"], (
        "a later check is another observation, not a correction of the first: "
        "they may be looking at different deployments"
    )


# ---- The sha defaults to what was actually delivered ----


async def test_sha_defaults_to_the_recorded_merge(
    client: AsyncClient, monkeypatch, db: aiosqlite.Connection
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)
    await repo.record_pipeline_merge(
        db, project_id=1, pr_number=999, task_id=task_id, merge_sha="recorded-merge-sha"
    )
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "done",
            "probe": "curl прод",
            "observation": "200",
        },
        headers=auth["agent"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sha"] == "recorded-merge-sha", (
        "what shipped is what gets observed"
    )


async def test_unknown_sha_stays_empty_rather_than_guessed(
    client: AsyncClient, monkeypatch
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    resp = await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={"outcome": "done", "probe": "curl прод", "observation": "200"},
        headers=auth["agent"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sha"] == "", (
        "no recorded merge means the hub does not know which build was seen — "
        "and an unknown sha is recorded as unknown (#725)"
    )


# ---- #814: the block, and the counter it has to appear in ----
#
# The point is not that a line of text appears. A block outside the coverage
# counter leaves the headline lying in the reassuring direction — it would say
# "5 of 6 blocks" while a seventh question went unasked — so these tests aim at
# checks_ran / checks_missing / checks_not_applicable, not at wording.


async def _delivered(db, task_id: int, sha: str = "merge-sha-live") -> str:
    await repo.record_pipeline_merge(
        db, project_id=1, pr_number=101, task_id=task_id, merge_sha=sha
    )
    await db.commit()
    return sha


async def test_missing_live_check_counts_as_no_signal(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)
    await _delivered(db, task_id)

    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["human"])
    ).json()
    block = brief["live_check"]
    assert block["state"] == "unknown"
    assert block["reason"], "an unknown state always carries its cause"
    missing = [c["check"] for c in brief["evidence_coverage"]["checks_missing"]]
    assert "live_check" in missing, (
        "counted, not merely printed: a block outside the counter makes the "
        "headline understate what was never checked"
    )


async def test_recorded_check_shows_what_was_seen(client: AsyncClient, monkeypatch, db):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)
    sha = await _delivered(db, task_id)
    await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "done",
            "probe": "GET /api/sessions на проде",
            "observation": "200 и непустой список",
            "sha": sha,
        },
        headers=auth["agent"],
    )

    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["human"])
    ).json()
    block = brief["live_check"]
    assert block["state"] == "done"
    assert block["probe"].startswith("GET /api/sessions")
    assert block["observation"].startswith("200")
    assert block["sha_mismatch"] is False
    assert "live_check" in brief["evidence_coverage"]["checks_ran"]

    page = await client.get(f"/tasks/{task_id}", headers=auth["human"])
    assert "Живая проверка" in page.text
    assert "GET /api/sessions на проде" in page.text
    assert "200 и непустой список" in page.text


async def test_nothing_to_observe_is_its_own_state(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)
    await _delivered(db, task_id)
    await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "not_applicable",
            "reason": "правка только в документации",
        },
        headers=auth["agent"],
    )

    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["human"])
    ).json()
    coverage = brief["evidence_coverage"]
    assert brief["live_check"]["state"] == "not_applicable"
    assert "live_check" in [c["check"] for c in coverage["checks_not_applicable"]]
    assert "live_check" not in [c["check"] for c in coverage["checks_missing"]], (
        "'nothing to observe' and 'nobody looked' are different claims, and a "
        "warning that inflates gets muted along with the real ones"
    )


async def test_evidence_from_another_sha_is_flagged(
    client: AsyncClient, monkeypatch, db
):
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)
    await _delivered(db, task_id, "merge-sha-live")
    await client.post(
        f"/api/tasks/{task_id}/live-check",
        json={
            "outcome": "done",
            "probe": "curl прод",
            "observation": "200",
            "sha": "some-older-build",
        },
        headers=auth["agent"],
    )

    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["human"])
    ).json()
    block = brief["live_check"]
    assert block["sha_mismatch"] is True
    assert block["reason"], "the mismatch is said out loud, not left to be noticed"
    assert "live_check" in [
        c["check"] for c in brief["evidence_coverage"]["checks_missing"]
    ], "an observation of another build is not evidence about what shipped"

    page = await client.get(f"/tasks/{task_id}", headers=auth["human"])
    assert "снято не на доставленном коммите" in page.text


async def test_undelivered_task_is_not_scolded_for_a_missing_check(
    client: AsyncClient, monkeypatch
):
    """Nothing has shipped yet, so a live check is not a check that failed."""
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    brief = (
        await client.get(f"/api/tasks/{task_id}/review-brief", headers=auth["human"])
    ).json()
    coverage = brief["evidence_coverage"]
    assert "live_check" in [c["check"] for c in coverage["checks_not_applicable"]]
    assert "live_check" not in [c["check"] for c in coverage["checks_missing"]], (
        "demanding evidence that could not exist yet is the inflated warning "
        "this counter was written to avoid (#534)"
    )


# ---- #837: "verified in production" needs production to have it ----
#
# 21.08.2026: task #823 was completed, its PR merged into develop, the deploy
# job skipped — and nothing stopped a live check claiming the panel had been
# observed working. The panel did not exist yet. Only ONE of the three delivery
# answers refuses here; ignorance is recorded, never used as a gate.

from tests.test_delivery_state import _use_real_git  # noqa: E402


async def _merged_task(client: AsyncClient, auth: dict, db, merge_sha: str) -> int:
    """A task the hub merged at ``merge_sha`` — the fact #534 records."""
    task_id = await _task(client, auth, title="Delivered?")
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 5000 + task_id, task_id, merge_sha),
    )
    await db.commit()
    return task_id


async def _post_check(client: AsyncClient, auth: dict, task_id: int, **body) -> object:
    return await client.post(
        f"/api/tasks/{task_id}/live-check", json=body, headers=auth["agent"]
    )


async def test_done_is_refused_before_deploy(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-1 (#837): merged, not released — the exact shape of the 21.08 defect.
    auth = _auth(monkeypatch)
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _merged_task(client, auth, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    resp = await _post_check(
        client,
        auth,
        task_id,
        outcome="done",
        probe="открыл страницу на проде",
        observation="всё работает",
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "not_deployed_yet"
    assert "ждёт релиза" in detail["message"], "the refusal must name the cause"
    assert await repo.list_live_checks(db, task_id) == [], (
        "a refused check must leave no record — a stored one would read as evidence"
    )


async def test_not_applicable_is_allowed_before_deploy(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#837): "there is nothing to observe" is a claim about the task and
    # does not depend on a release having happened.
    auth = _auth(monkeypatch)
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _merged_task(client, auth, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    resp = await _post_check(
        client, auth, task_id, outcome="not_applicable", reason="меняются только тесты"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "not_applicable"


async def test_done_is_allowed_after_deploy(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#837): once the merge is in what production runs, nothing changes
    # for the caller — the check exists to stop one case, not to add friction.
    auth = _auth(monkeypatch)
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _merged_task(client, auth, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    resp = await _post_check(
        client,
        auth,
        task_id,
        outcome="done",
        probe="GET /tasks/1 на проде",
        observation="строка доставки на месте",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deploy_state"] == "in_prod"


async def test_unknown_deploy_state_does_not_block(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#837): an installation with no delivery facts knows nothing about
    # production. Refusing there would turn ignorance into a gate — the record
    # is kept and marked instead, so it never reads as verified.
    auth = _auth(monkeypatch)
    task_id = await _task(client, auth)

    resp = await _post_check(
        client,
        auth,
        task_id,
        outcome="done",
        probe="смотрел вручную",
        observation="работает",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["deploy_state"] == "unknown", (
        "accepted, but visibly not checked against a deploy"
    )
    page = (await client.get(f"/tasks/{task_id}", headers=auth["human"])).text
    assert "не сверено с выкатом" in page
