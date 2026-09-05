"""Контракт hub_submit_steward_judgement: закрытые словари и at-most-once (#1022).

Спека: docs/specs/steward-agent.md §5, §6.1, §7. Только запись — без
транзишена. События аудита пишет #1023; применение суждения — F4.

AC-1: нет verdict → 422 с reason и hint, ряд не появляется.
AC-2: ground.source и escalate_reason вне закрытых словарей → 422; unknown нет.
AC-3: confidence=low побеждает verdict=approve → escalate / low_confidence.
AC-4: вторая запись той же тройки (task_id, generation, kind) отвергается.
AC-5: closure.finding_uid должен быть в отчёте этой генерации.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hub import config, repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.services import admin as admin_svc
from hub.services.finding_identity import finding_uids

STEWARD_TOKEN = "steward-env-token"  # pragma: allowlist secret

_FINDING = {
    "locator": "lines",
    "title": "policy JSON error path untested",
    "severity": "medium",
    "category": "tests",
    "file": "hub/web.py",
    "start_line": 633,
}


@pytest.fixture
async def hub(client, db, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {STEWARD_TOKEN: TokenIdentity("steward-env", "steward")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = await admin_svc.create_principal(
        db, kind="human", username="alice", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    steward = await admin_svc.create_principal(
        db, kind="agent", username="steward-bot", role_slug="steward"
    )
    steward_key = await admin_svc.create_api_key(db, steward["id"], name="steward-key")
    return SimpleNamespace(
        client=client,
        db=db,
        human_auth={"Authorization": f"Bearer {human_key['plaintext_key']}"},
        steward_auth={"Authorization": f"Bearer {steward_key['plaintext_key']}"},
    )


async def _make_task(hub) -> int:
    resp = await hub.client.post(
        "/api/tasks",
        json={"title": "черновик для суждения"},
        headers=hub.human_auth,
    )
    assert resp.status_code in {200, 201}, resp.text
    return resp.json()["id"]


def _payload(**overrides):
    body = {
        "generation": 1,
        "kind": "verdict",
        "verdict": "approve",
        "confidence": "high",
        "grounds": [{"source": "ci_pinned_sha"}],
    }
    body.update(overrides)
    return body


async def _post(hub, task_id: int, body: dict):
    return await hub.client.post(
        f"/api/tasks/{task_id}/steward-judgement",
        json=body,
        headers=hub.steward_auth,
    )


async def _count(hub, task_id: int) -> int:
    rows = await fetchall(
        hub.db,
        "SELECT COUNT(*) AS n FROM steward_judgements WHERE task_id=?",
        (task_id,),
    )
    return int(rows[0]["n"])


def _detail(resp) -> dict:
    payload = resp.json()
    detail = payload.get("detail", payload)
    assert isinstance(detail, dict), payload
    return detail


@pytest.mark.asyncio
async def test_verdict_is_required(hub):
    """AC-1: omitting verdict is 422 with reason and hint; nothing is stored."""
    task_id = await _make_task(hub)
    body = _payload()
    del body["verdict"]
    resp = await _post(hub, task_id, body)
    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail.get("reason")
    assert detail.get("hint")
    assert await _count(hub, task_id) == 0


@pytest.mark.asyncio
async def test_closed_vocabularies_enforced(hub):
    """AC-2: ground.source and escalate_reason outside the closed sets are 422."""
    task_id = await _make_task(hub)

    bad_source = await _post(
        hub, task_id, _payload(grounds=[{"source": "gut_feeling"}])
    )
    assert bad_source.status_code == 422, bad_source.text
    source_detail = _detail(bad_source)
    allowed_sources = source_detail.get("allowed") or []
    assert "ci_pinned_sha" in allowed_sources
    assert "unknown" not in allowed_sources
    assert await _count(hub, task_id) == 0

    bad_reason = await _post(
        hub,
        task_id,
        _payload(verdict="escalate", escalate_reason="unknown", confidence="high"),
    )
    assert bad_reason.status_code == 422, bad_reason.text
    reason_detail = _detail(bad_reason)
    allowed_reasons = reason_detail.get("allowed") or []
    assert "low_confidence" in allowed_reasons
    assert "unknown" not in allowed_reasons
    assert await _count(hub, task_id) == 0


@pytest.mark.asyncio
async def test_low_confidence_beats_verdict(hub):
    """AC-3: approve + confidence=low is stored as escalate / low_confidence."""
    task_id = await _make_task(hub)
    resp = await _post(hub, task_id, _payload(verdict="approve", confidence="low"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "escalate"
    assert data["escalate_reason"] == "low_confidence"
    assert data["submitted_verdict"] == "approve"
    row = await repo.get_steward_judgement(hub.db, task_id, 1, "verdict")
    assert row is not None
    assert row["verdict"] == "escalate"
    assert row["escalate_reason"] == "low_confidence"
    assert row["submitted_verdict"] == "approve"


@pytest.mark.asyncio
async def test_at_most_once_per_generation(hub):
    """AC-4: the second judgement of the same triple is refused, not overwritten."""
    task_id = await _make_task(hub)
    first = await _post(hub, task_id, _payload(verdict="approve", model="first"))
    assert first.status_code == 200, first.text
    second = await _post(
        hub, task_id, _payload(verdict="changes_requested", model="second")
    )
    assert second.status_code in {409, 422}, second.text
    assert await _count(hub, task_id) == 1
    row = await repo.get_steward_judgement(hub.db, task_id, 1, "verdict")
    assert row["submitted_verdict"] == "approve"
    assert row["model"] == "first"


@pytest.mark.asyncio
async def test_closure_uid_must_exist_in_report(hub):
    """AC-5: a closure cannot name a finding_uid absent from this generation."""
    task_id = await _make_task(hub)
    await repo.insert_machine_review(
        hub.db,
        task_id=task_id,
        submission_generation=1,
        findings_confirmed=json.dumps([_FINDING]),
        incomplete=False,
    )
    await hub.db.commit()
    known_uid = finding_uids([_FINDING])[0]

    missing = await _post(
        hub,
        task_id,
        _payload(
            closures=[{"finding_uid": "deadbeefdeadbeef", "type": "fixed"}],
        ),
    )
    assert missing.status_code == 422, missing.text
    detail = _detail(missing)
    assert "deadbeefdeadbeef" in (detail.get("message") or "") + (
        detail.get("hint") or ""
    )
    assert await _count(hub, task_id) == 0

    ok = await _post(
        hub,
        task_id,
        _payload(closures=[{"finding_uid": known_uid, "type": "fixed"}]),
    )
    assert ok.status_code == 200, ok.text
    assert await _count(hub, task_id) == 1


_UNRESOLVED = {
    "title": "адъюдикаторы разошлись про порядок отказов",
    "why": "один считает путь недостижимым, другой — живым",
}


@pytest.mark.asyncio
async def test_closure_uid_may_come_from_the_unresolved_section(hub):
    """#1170: uid неразрешённой находки — известный uid, а не unknown.

    Пока множество известных строилось по одному findings_confirmed,
    привратник применения требовал закрытий по разделу unresolved, а контракт
    такие закрытия отклонял 422. Правило было невыполнимым по контракту:
    отказать стюард мог, а закрыть — нет.
    """
    from hub.services.finding_identity import unresolved_uids

    task_id = await _make_task(hub)
    await repo.insert_machine_review(
        hub.db,
        task_id=task_id,
        submission_generation=1,
        findings_confirmed=json.dumps([]),
        unresolved=json.dumps([_UNRESOLVED]),
        incomplete=False,
    )
    await hub.db.commit()

    ok = await _post(
        hub,
        task_id,
        _payload(
            closures=[
                {
                    "finding_uid": unresolved_uids([_UNRESOLVED])[0],
                    "type": "author_outcome",
                }
            ]
        ),
    )

    assert ok.status_code == 200, ok.text
    assert await _count(hub, task_id) == 1
