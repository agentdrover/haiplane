"""Доказательства для вердикта: суждение стюарда, отчёт поколения, доверие CI (#1697)."""

from __future__ import annotations

import argparse
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub import cli, config, mcp_server
from hub import repository as repo
from hub.config import TokenIdentity
from hub.mcp_server import HubApiError, hub_get_review_brief
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services import verdict_evidence
from hub.services.steward_evidence import build_evidence_packet, packet_payload
from tests.ci_trust_support import VERIFIED_ROW
from tests.state_support import make_state_task

PATH = "/api/tasks/{}/verdict-evidence"
SHA1, SHA2 = "a" * 40, "b" * 40
JUDGED = "2026-10-15 10:00:00"
SECRET_TEXT = "СУЖДЕНИЕ-СТЮАРДА-УНИКАЛЬНЫЙ-ТЕКСТ"  # marker for AC-4


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def hub(client, db, monkeypatch):
    """Хаб с включённым auth; оператор и env-токены human/agent."""
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "env-human": TokenIdentity("env-h", "human"),
            "env-agent": TokenIdentity("env-a", "agent"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    op = await admin_svc.create_principal(
        db, kind="human", username="op", role_slug="operator"
    )
    key = await admin_svc.create_api_key(db, op["id"], name="k")
    return SimpleNamespace(
        client=client,
        db=db,
        op_id=op["id"],
        op=_bearer(key["plaintext_key"]),
    )


async def _role_headers(hub, role: str) -> dict[str, str]:
    principal = await admin_svc.create_principal(
        hub.db, kind="agent", username=f"p-{role}", role_slug=role
    )
    key = await admin_svc.create_api_key(hub.db, principal["id"], name=role)
    return _bearer(key["plaintext_key"])


async def _task(hub, *, generation: int = 2, status: str = "review") -> int:
    resp = await hub.client.post(
        "/api/tasks", json={"title": "доказательства"}, headers=hub.op
    )
    assert resp.status_code in (200, 201), resp.text
    task_id = resp.json()["id"]
    for gen, sha in ((1, SHA1), (2, SHA2)):
        if gen <= generation:
            await repo.record_submission(
                hub.db, task_id=task_id, generation=gen, sha=sha, base_branch="main"
            )
    await repo.update_task(
        hub.db,
        task_id,
        status=status,
        submission_generation=generation,
        submission_sha=SHA2 if generation >= 2 else SHA1,
    )
    await hub.db.commit()
    return task_id


async def _judgement(db, task_id: int, generation: int, grounds: list, reason="r"):
    await repo.insert_steward_judgement(
        db,
        task_id=task_id,
        generation=generation,
        kind="verdict",
        submitted_verdict="approved",
        verdict="escalate",
        confidence="high",
        escalate_reason=reason,
        grounds=json.dumps(grounds),
        model="gpt-x",
    )
    await db.execute(
        "UPDATE steward_judgements SET created_at=? WHERE task_id=? AND generation=?",
        (JUDGED, task_id, generation),
    )
    await db.commit()


async def _review(db, task_id: int, generation: int, **kw) -> None:
    await repo.insert_machine_review(
        db, task_id=task_id, submission_generation=generation, **kw
    )
    await db.commit()


async def _ci(db, task_id: int, sha: str, *, reported: str, checked: str | None):
    """Проекция коммита и строка ЕЁ попытки с заданными временами."""
    await repo.upsert_ci_run_report(
        db,
        task_id=task_id,
        head_sha=sha,
        ac_results="[]",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
        **{**VERIFIED_ROW, "attempt_key": "ci:7:1"},
    )
    await repo.upsert_ci_report_attempt(
        db,
        task_id=task_id,
        head_sha=sha,
        run_id="7",
        run_attempt="1",
        ac_results="{}",
        checks="{}",
        validation_status="pass",
        validation_log="",
        reason="",
        reported_by="ci",
        mutations=None,
        baseline=None,
        provenance="{}",
        submitted="{}",
    )
    await db.execute(
        "UPDATE ci_report_attempts SET reported_at=?, checked_at=?, "
        "trust='verified', trust_reason='' WHERE task_id=? AND head_sha=?",
        (reported, checked, task_id, sha),
    )
    # The projection's own time is later than the attempt's: reading it
    # instead of the attempt's checked_at would give a different answer.
    await db.execute(
        "UPDATE ci_run_reports SET reported_at='2026-10-15 12:00:00' "
        "WHERE task_id=? AND head_sha=?",
        (task_id, sha),
    )
    await db.commit()


# --------------------------------------------------------------------------- AC-1


async def test_evidence_is_of_current_generation_and_bounded(hub):
    """AC-1: поколение 2, а не последний отчёт задачи; урезание; попытка CI."""
    task_id = await _task(hub)
    grounds = [{"source": "ci_pinned_sha", "detail": "x" * 2000 + "\nвторая"}] * 50
    await _judgement(hub.db, task_id, 2, grounds, reason="нужен\nчеловек")
    await _judgement(hub.db, task_id, 1, [], reason="ПРОШЛОЕ")
    # Отчёт поколения 2 записан РАНЬШЕ позднего отчёта поколения 1.
    await _review(
        hub.db,
        task_id,
        2,
        raw_count=4,
        findings_confirmed=json.dumps([{"title": "a"}]),
        findings_rejected=json.dumps([{"title": "b"}, {"title": "c"}]),
        unresolved=json.dumps([{"x": 1}, {"x": 2}, {"x": 3}]),
        profile="deep",
        model="m2",
    )
    await _review(hub.db, task_id, 1, raw_count=99, model="old", incomplete=False)
    await _ci(
        hub.db,
        task_id,
        SHA2,
        reported="2026-10-15 09:00:00",
        checked="2026-10-15 10:30:00",
    )

    resp = await hub.client.get(PATH.format(task_id), headers=hub.op)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["generation"] == 2
    steward = body["steward"]
    assert steward["escalate_reason"] == "нужен человек"
    assert len(steward["grounds"]) == 5 and steward["truncated"] is True
    assert all(len(g["detail"]) <= 300 for g in steward["grounds"])
    assert all("\n" not in g["detail"] for g in steward["grounds"])
    assert steward["judged_at"] == "2026-10-15T10:00:00+00:00"
    assert "ПРОШЛОЕ" not in json.dumps(body, ensure_ascii=False)
    machine = body["machine_review"]
    assert (machine["raw_count"], machine["model"], machine["profile"]) == (
        4,
        "m2",
        "deep",
    )
    assert (machine["confirmed"], machine["rejected"], machine["unresolved"]) == (
        1,
        2,
        3,
    )
    assert machine["incomplete"] is None
    ci = body["ci"]
    assert ci["trust"] == "verified" and ci["validation_status"] == "pass"
    assert ci["reported_at"] == "2026-10-15T09:00:00+00:00"
    assert ci["checked_at"] == "2026-10-15T10:30:00+00:00"
    assert ci["attempt_pending"] is False

    # Ключ попытки без строки попытки: время неизвестно, а не взято у проекции.
    await hub.db.execute(
        "UPDATE ci_run_reports SET attempt_key='ci:9:9' WHERE task_id=?", (task_id,)
    )
    await hub.db.commit()
    pending = (await hub.client.get(PATH.format(task_id), headers=hub.op)).json()["ci"]
    assert pending["attempt_pending"] is True
    assert pending["reported_at"] is None and pending["checked_at"] is None

    missing = await hub.client.get(PATH.format(9999), headers=hub.op)
    assert missing.status_code == 404


# --------------------------------------------------------------------------- AC-2


async def test_verdict_evidence_access_matrix(hub):
    """AC-2: три DB-роли и env human/agent читают; остальные получают 403."""
    task_id = await _task(hub)
    # Роль watcher с правом в строке роли: право есть, а доступа всё равно нет.
    await hub.db.execute(
        "INSERT INTO role_permissions (role_id, permission) "
        "SELECT id, 'verdict.evidence.read' FROM roles WHERE slug='watcher'"
    )
    await hub.db.commit()

    allowed = {
        "admin": await _role_headers(hub, "admin"),
        "operator": await _role_headers(hub, "operator"),
        "agent": await _role_headers(hub, "agent"),
        "env-human": _bearer("env-human"),
        "env-agent": _bearer("env-agent"),
    }
    denied = {
        role: await _role_headers(hub, role)
        for role in (
            "developer",
            "viewer",
            "security_admin",
            "reviewer_agent",
            "ci_runner",
            "watcher",
            "ci_reader",
            "steward",
        )
    }
    # Сессии chat-pair: исполнитель возвращённой задачи и ревьюер сдачи.
    returned = await _task(hub, generation=1, status="fix_requested")
    code, _ = await cp.issue_run_code(
        hub.db, returned, 2, issued_by_principal_id=hub.op_id
    )
    reviewing = await _task(hub, generation=1)
    rcode, _ = await cp.issue_code(
        hub.db,
        hub.op_id,
        kind="reviewer",
        bound_task_id=reviewing,
        bound_generation=1,
    )
    await hub.db.commit()
    sessions = {}
    for name, c, tid in (
        ("implementer", code, returned),
        ("reviewer", rcode, reviewing),
    ):
        resp = await hub.client.post(
            "/api/auth/chat-pair/redeem",
            json={"code": c},
            headers={"x-forwarded-for": "203.0.113.9"},
        )
        assert resp.status_code == 200, resp.text
        sessions[name] = (_bearer(resp.json()["token"]), tid)

    for name, headers in allowed.items():
        resp = await hub.client.get(PATH.format(task_id), headers=headers)
        assert resp.status_code == 200, (name, resp.text)
    for name, headers in denied.items():
        resp = await hub.client.get(PATH.format(task_id), headers=headers)
        assert resp.status_code == 403, (name, resp.status_code, resp.text)
    for name, (headers, tid) in sessions.items():
        resp = await hub.client.get(PATH.format(tid), headers=headers)
        assert resp.status_code == 403, (name, resp.status_code, resp.text)


# --------------------------------------------------------------------------- AC-3


def _brief_stub() -> dict:
    return {
        "task_id": 42,
        "title": "T",
        "status": "review",
        "submission_generation": 2,
    }


async def _mcp_text(hub, task_id: int, *, refused: bool = False) -> str:
    async def fake(path: str, **_kw):
        if path.endswith("/review-brief"):
            return _brief_stub()
        if refused:
            raise HubApiError(
                {"reason": "permission_denied", "message": "missing permission"}
            )
        return json.loads(json.dumps(await verdict_evidence.build(hub.db, task_id)))

    with patch.object(mcp_server, "_api_get", AsyncMock(side_effect=fake)):
        out = await hub_get_review_brief(task_id, section="full")
    return json.loads(out.content[0].text)["message"]


async def test_brief_text_and_cli_show_verdict_block(hub):
    """AC-3: блок в тексте MCP и в CLI; пометка «судил до CI»; state; отказ роли."""
    task_id = await _task(hub)
    await _judgement(hub.db, task_id, 2, [{"source": "ci_pinned_sha", "detail": "д"}])
    await _review(hub.db, task_id, 2, raw_count=4, incomplete=False)
    # Попытка отчитана ДО суждения, подтверждена ПОСЛЕ: ловит reported_at.
    await _ci(
        hub.db,
        task_id,
        SHA2,
        reported="2026-10-15 09:00:00",
        checked="2026-10-15 10:30:00",
    )

    text = await _mcp_text(hub, task_id)
    assert "Для вердикта (поколение 2)" in text
    assert "raw 4 → confirmed 0 / rejected 0 / unresolved 0" in text
    assert "trust verified" in text and "ci:7:1" in text
    assert "ci_pinned_sha: д" in text
    assert "стюард судил до подтверждения CI (10:00 → 10:30)" in text

    # CI подтверждён ДО суждения: пометки нет.
    await hub.db.execute(
        "UPDATE ci_report_attempts SET checked_at='2026-10-15 09:30:00'"
    )
    await hub.db.commit()
    assert "стюард судил до подтверждения CI" not in await _mcp_text(hub, task_id)

    # Задача-состояние.
    state_id = await make_state_task(hub.db)
    await repo.update_task(hub.db, state_id, submission_generation=1)
    await hub.db.commit()
    state_text = await _mcp_text(hub, state_id)
    assert state_text.count("не применимо") >= 2

    # Роль без права.
    refused = await _mcp_text(hub, task_id, refused=True)
    assert "доказательства недоступны этой роли" in refused

    # CLI: тот же формат; 403 -> та же строка роли.
    evidence = await verdict_evidence.build(hub.db, task_id)
    await hub.db.execute(
        "UPDATE ci_report_attempts SET checked_at='2026-10-15 10:30:00'"
    )
    await hub.db.commit()
    evidence = await verdict_evidence.build(hub.db, task_id)
    reply = MagicMock()
    reply.__enter__.return_value.read.return_value = json.dumps(evidence).encode()
    out = StringIO()
    with (
        patch.object(cli.urllib.request, "urlopen", return_value=reply),
        patch("sys.stdout", new=out),
    ):
        assert cli.cmd_verdict_evidence(argparse.Namespace(task_id=task_id)) == 0
    assert "стюард судил до подтверждения CI (10:00 → 10:30)" in out.getvalue()
    assert out.getvalue().strip() == verdict_evidence.format_block(evidence)

    import urllib.error

    forbidden = urllib.error.HTTPError("u", 403, "no", {}, None)  # type: ignore[arg-type]
    with patch.object(cli.urllib.request, "urlopen", side_effect=forbidden):
        assert "доступны этой роли" in cli._verdict_block(task_id)

    # review-brief печатает блок рядом с брифом.
    err = StringIO()
    with (
        patch.object(cli, "_api", MagicMock(return_value=_brief_stub())),
        patch.object(cli, "_verdict_block", MagicMock(return_value="БЛОК")),
        patch("sys.stdout", new=StringIO()),
        patch("sys.stderr", new=err),
    ):
        cli.cmd_review_brief(argparse.Namespace(task_id=task_id, full=True))
    assert "БЛОК" in err.getvalue()


# --------------------------------------------------------------------------- AC-4


async def test_steward_packet_does_not_carry_its_judgement(hub):
    """AC-4: пакет стюарда — фиксированный набор ключей и без текста суждения."""
    task_id = await _task(hub)
    await _judgement(
        hub.db,
        task_id,
        2,
        [{"source": "ci_pinned_sha", "detail": SECRET_TEXT}],
        reason=SECRET_TEXT,
    )
    packet = await build_evidence_packet(hub.db, task_id, 2)
    payload = packet_payload(packet)

    assert set(payload) == {
        "task_id",
        "generation",
        "brief",
        "facts",
        "absent_sources",
        "quotes",
        "injection_suspected",
        "injection_signals",
    }
    dumped = json.dumps(payload, ensure_ascii=False)
    assert SECRET_TEXT not in dumped
    assert "escalate_reason" not in dumped and "grounds" not in dumped.replace(
        "gate_grounds", ""
    )
