"""Задача-состояние (#1647): единицы, на которых держатся десять AC.

Десять тестов AC живут в файлах своих областей и проверяют поведение целиком.
Здесь — то, что удобнее и точнее проверять по частям: точные шаблоны сканера
секретов, контракт доказательств, предикат, insert-only таблица, отпечаток
постановки, условная запись вердикта, веб-форма и CLI.
"""

from __future__ import annotations

import json
import logging

import aiosqlite
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import repository as repo
from hub.services import secret_values, state_evidence
from hub.services.result_kind import (
    automation_not_applicable,
    is_state,
    qualifying_ac_count,
    result_kind_of,
)
from tests.state_support import (
    MARKER,
    auth,
    drive_to_review,
    evidence_for,
    evidence_rows,
    fingerprint,
    make_project,
    make_state_task,
    pair_start,
    submit,
)

# --- сканер значений: точные шаблоны ----------------------------------------

_SECRET_SAMPLES = {
    "openai_style_key": "sk-" + "a1B2c3D4e5F6g7H8i9",
    "github_token": "ghp_" + "a1B2c3D4e5F6" * 3,  # pragma: allowlist secret
    "github_pat": "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1",
    "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "slack_token": "xoxb-" + "123456789012-abcdefghij",
    "bearer_token": "Authorization: Bearer " + "abcdef0123456789abcdef",
    "private_key_block": "-----BEGIN RSA PRIVATE KEY-----",  # pragma: allowlist secret
    "assigned_secret": "API_TOKEN=abc123",  # pragma: allowlist secret
}

_CLEAN_SAMPLES = (
    "dig +short A example.org -> 203.0.113.7",
    "ghp_short",  # короче 36
    "sk-short",
    "AKIA123",  # короче
    "Bearer",  # слово без токена
    "PASSWORD=",  # присваивание без значения
    "ключ принят, ответ 200",
    "-----BEGIN CERTIFICATE-----",  # сертификат — не приватный ключ
    "",
)


@pytest.mark.parametrize("name", sorted(_SECRET_SAMPLES))
def test_the_scanner_names_each_exact_pattern(name: str):
    assert secret_values.find_secret(_SECRET_SAMPLES[name]) == name
    assert _SECRET_SAMPLES[name] not in name, "имя шаблона не содержит значения"


@pytest.mark.parametrize("text", _CLEAN_SAMPLES)
def test_the_scanner_lets_depersonalised_observations_through(text: str):
    assert secret_values.find_secret(text) == ""


def test_every_pattern_has_a_sample_so_none_is_dead():
    assert {name for name, _ in secret_values.PATTERNS} == set(_SECRET_SAMPLES)


# --- контракт доказательств --------------------------------------------------

_KNOWN = ("AC-1", "AC-2")


def _kind(raw, known=_KNOWN) -> state_evidence.EvidenceError:
    with pytest.raises(state_evidence.EvidenceError) as caught:
        state_evidence.validate_evidence(raw, known)
    return caught.value


def test_a_full_set_is_normalised_and_stripped():
    raw = evidence_for(observed="  ответ  ")
    out = state_evidence.validate_evidence(raw, _KNOWN)
    assert [r["ac_id"] for r in out] == ["AC-1", "AC-2"]
    assert out[0]["observed"] == "ответ"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("не список", state_evidence.KIND_TYPE),
        (None, state_evidence.KIND_TYPE),
        ([], state_evidence.KIND_EMPTY_SET),
        (["строка"], state_evidence.KIND_TYPE),
        ([{"ac_id": "AC-1"}], state_evidence.KIND_MISSING_FIELD),
        ([dict(evidence_for()[0], extra="x")], state_evidence.KIND_UNKNOWN_FIELD),
        ([dict(evidence_for()[0], observed=5)], state_evidence.KIND_TYPE),
        ([dict(evidence_for()[0], target="  ")], state_evidence.KIND_EMPTY_VALUE),
        ([dict(evidence_for()[0], action="x" * 1001)], state_evidence.KIND_TOO_LONG),
        ([dict(evidence_for()[0], observed_at="x" * 65)], state_evidence.KIND_TOO_LONG),
        (evidence_for() + evidence_for()[:1], state_evidence.KIND_DUPLICATE_AC),
        (evidence_for(("AC-1", "AC-2", "AC-3")), state_evidence.KIND_UNKNOWN_AC),
        (evidence_for(("AC-1",)), state_evidence.KIND_MISSING_AC),
        ([dict(evidence_for()[0]) for _ in range(51)], state_evidence.KIND_TOO_MANY),
    ],
)
def test_each_contract_violation_has_its_own_kind(raw, kind):
    assert _kind(raw).kind == kind


def test_the_limits_are_inclusive():
    ok = [
        dict(
            item,
            action="a" * state_evidence.LIMITS["action"],
            observed="o" * state_evidence.LIMITS["observed"],
            target="t" * state_evidence.LIMITS["target"],
            observed_at="d" * state_evidence.LIMITS["observed_at"],
        )
        for item in evidence_for()
    ]
    assert len(state_evidence.validate_evidence(ok, _KNOWN)) == 2


def test_an_error_never_carries_a_value_and_names_only_safe_acs():
    token = "ghp_" + "a1B2c3D4e5F6" * 3  # pragma: allowlist secret
    bad = [dict(evidence_for()[0], observed=f"{MARKER} {token}")]
    err = _kind(bad + evidence_for(("AC-2",)))
    assert err.kind == state_evidence.KIND_CREDENTIAL
    assert err.ac_ids == ["AC-1"] and err.field == "observed"
    blob = json.dumps([err.kind, err.ac_ids, err.field, err.pattern, str(err)])
    assert MARKER not in blob and "ghp_" not in blob

    weird = _kind([dict(evidence_for()[0], ac_id=MARKER, observed="")])
    assert weird.ac_ids == [], "идентификатор не вида AC-N не эхом не возвращается"
    assert MARKER not in json.dumps([weird.kind, weird.ac_ids, weird.field])


# --- предикат ---------------------------------------------------------------


def test_the_predicate_reads_dicts_models_and_missing_columns():
    from hub.models import TaskCreate

    assert is_state({"result_kind": "state"}) and automation_not_applicable(
        {"result_kind": "state"}
    )
    assert not is_state({"result_kind": "commit"})
    assert result_kind_of({}) == "commit", "нет колонки — commit"
    assert result_kind_of(None) == "commit"
    assert is_state(TaskCreate(title="x", result_kind="state"))
    assert not is_state(TaskCreate(title="x"))
    assert (
        qualifying_ac_count(
            [
                {"verifiable_by": "test"},
                {"verifiable_by": "manual"},
                {"verifiable_by": "ui_check"},
            ]
        )
        == 2
    )


async def test_the_predicate_reads_a_row_by_task_id(db: aiosqlite.Connection):
    from hub.services.result_kind import task_automation_not_applicable

    state_id = await make_state_task(db)
    commit_id = await make_state_task(db, result_kind="commit")
    assert await task_automation_not_applicable(db, state_id)
    assert not await task_automation_not_applicable(db, commit_id)
    assert not await task_automation_not_applicable(db, 99999)


# --- схема ------------------------------------------------------------------


async def test_columns_default_to_commit_and_empty_rollback(db: aiosqlite.Connection):
    commit_id = await make_state_task(db, result_kind="commit", rollback=None)
    row = dict(await repo.get_task(db, commit_id))
    assert row["result_kind"] == "commit" and row["rollback"] == ""
    state_id = await make_state_task(db)
    assert dict(await repo.get_task(db, state_id))["result_kind"] == "state"


async def test_task_evidence_is_insert_only(db: aiosqlite.Connection):
    task_id = await make_state_task(db)
    await repo.insert_task_evidence(
        db,
        task_id=task_id,
        generation=1,
        items=state_evidence.validate_evidence(evidence_for(), _KNOWN),
        principal_id=7,
        agent="sig",
    )
    await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "UPDATE task_evidence SET observed='иначе' WHERE task_id=?", (task_id,)
        )
    await db.rollback()
    with pytest.raises(aiosqlite.IntegrityError):
        await repo.insert_task_evidence(
            db,
            task_id=task_id,
            generation=1,
            items=state_evidence.validate_evidence(
                evidence_for(("AC-1", "AC-2")), _KNOWN
            ),
            principal_id=7,
            agent="sig",
        )
    await db.rollback()
    assert len(await evidence_rows(db, task_id)) == 2
    assert {r["observed"] for r in await evidence_rows(db, task_id)} != {"иначе"}


async def test_the_statement_fingerprint_moves_for_state_and_not_for_commit(
    db: aiosqlite.Connection,
):
    from hub.services.statement_generation import statement_fingerprint

    commit_id = await make_state_task(db, result_kind="commit", rollback=None)
    before = await statement_fingerprint(db, commit_id)
    await db.execute("UPDATE tasks SET rollback='что-то' WHERE id=?", (commit_id,))
    await db.commit()
    assert await statement_fingerprint(db, commit_id) == before, (
        "rollback у commit-задачи — не постановка: отпечаток прежний"
    )
    state_id = await make_state_task(db)
    first = await statement_fingerprint(db, state_id)
    await db.execute("UPDATE tasks SET rollback='иной откат' WHERE id=?", (state_id,))
    await db.commit()
    assert await statement_fingerprint(db, state_id) != first


# --- условная запись вердикта ------------------------------------------------


async def test_the_state_decision_is_conditional_in_sql(db: aiosqlite.Connection):
    task_id = await make_state_task(db)
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=2 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    kwargs = dict(verdict="approved", findings_json="[]", new_status="completed")
    assert not await repo.record_state_decision(db, task_id, generation=1, **kwargs)
    assert dict(await repo.get_task(db, task_id))["status"] == "review"
    commit_id = await make_state_task(db, result_kind="commit")
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=1 WHERE id=?",
        (commit_id,),
    )
    await db.commit()
    assert not await repo.record_state_decision(
        db, commit_id, generation=1, **kwargs
    ), "условие result_kind='state' стоит в самом SQL"
    assert await repo.record_state_decision(db, task_id, generation=2, **kwargs)
    done = dict(await repo.get_task(db, task_id))
    assert done["status"] == "completed" and done["completed_at"]
    assert done["submission_generation"] == 2, "поколение не растёт"
    assert not await repo.record_state_decision(db, task_id, generation=2, **kwargs), (
        "повтор: статус уже не review"
    )


# --- сдача и вердикт: частности ---------------------------------------------


async def test_a_state_submission_needs_a_summary_and_a_complete_statement(
    client: AsyncClient, db: aiosqlite.Connection
):
    task_id = await make_state_task(db)
    assert (await pair_start(client, task_id)).status_code == 200
    before = await fingerprint(db, task_id)
    no_summary = await submit(client, task_id, evidence_for(), summary="  ")
    assert no_summary.status_code == 422
    assert no_summary.json()["detail"]["reason"] == "state_summary_required"
    assert await fingerprint(db, task_id) == before

    # DoR обошли force-ом: на сдаче постановка проверяется заново.
    await db.execute("UPDATE tasks SET rollback='' WHERE id=?", (task_id,))
    await db.commit()
    gap = await submit(client, task_id, evidence_for())
    assert gap.status_code == 422
    assert gap.json()["detail"]["reason"] == "state_statement_incomplete"
    assert "rollback" in gap.json()["detail"]["message"]
    assert await fingerprint(db, task_id) == before


async def test_the_contract_mode_decides_about_the_model_only(
    client: AsyncClient, db: aiosqlite.Connection
):
    pid = await make_project(db, "st-contract", {"submission_contract": "require"})
    task_id = await make_state_task(db, project_id=pid)
    assert (await pair_start(client, task_id)).status_code == 200
    refused = await submit(client, task_id, evidence_for(), model="")
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "submission_contract_violated"
    assert "mutation" not in json.dumps(refused.json()["detail"]["violations"]), (
        "мутации к задаче-состоянию не применяются"
    )
    ok = await submit(client, task_id, evidence_for())
    assert ok.status_code == 200, ok.text


async def test_an_agent_changes_requested_needs_the_generation_too(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    headers = auth(monkeypatch)
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id, headers=headers["impl"])
    before = await fingerprint(db, task_id)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "changes_requested", "comments": "нужно ещё"},
        headers=headers["rev"],
    )
    assert resp.status_code == 409, resp.text
    assert await fingerprint(db, task_id) == before
    ok = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "changes_requested", "comments": "нужно ещё",
              "expected_generation": 1},
        headers=headers["rev"],
    )  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "running"


async def test_a_resubmission_from_review_opens_a_new_generation(
    client: AsyncClient, db: aiosqlite.Connection
):
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    again = await submit(client, task_id, evidence_for(observed="новое наблюдение"))
    assert again.status_code == 200 and again.json()["submission_generation"] == 2
    assert {r["generation"] for r in await evidence_rows(db, task_id)} == {1, 2}
    assert dict(await repo.get_task(db, task_id))["review_verdict"] is None


async def test_approval_holds_on_a_live_blocker_and_on_a_missing_prevention(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Прод-дефект без вывода и блокер после сдачи удерживают завершение."""
    headers = auth(monkeypatch)
    blocked = await make_state_task(db, title="с блокером")
    await drive_to_review(client, db, blocked, headers=headers["impl"])
    await repo.add_task_update(
        db, blocked, "dev", "blocker", "ждём окно у регистратора"
    )

    prod = await make_state_task(db, title="прод-дефект")
    await db.execute("UPDATE tasks SET found_in='prod' WHERE id=?", (prod,))
    await db.commit()
    assert (await pair_start(client, prod, headers=headers["impl"])).status_code == 200
    sent = await submit(
        client,
        prod,
        evidence_for(),
        headers=headers["impl"],
        prevention={
            "kind": "accepted_risk",
            "reason": "разовая операция",
            "revisit": "2027-01",
        },
    )
    assert sent.status_code == 200, sent.text
    await db.execute("UPDATE tasks SET defect_prevention='' WHERE id=?", (prod,))
    await db.commit()

    for task_id in (blocked, prod):
        resp = await client.post(
            f"/api/tasks/{task_id}/review-verdict",
            json={"verdict": "approved", "expected_generation": 1},
            headers=headers["human"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "needs_decision", task_id
        assert dict(await repo.get_task(db, task_id))["review_verdict"] == "approved"
        assert not await db.execute_fetchall(
            "SELECT 1 FROM events WHERE task_id=? AND kind='task_completed'", (task_id,)
        )
        reasons = [
            json.loads(e["payload"]).get("reason")
            for e in await db.execute_fetchall(
                "SELECT payload FROM events WHERE task_id=? AND kind='needs_decision'",
                (task_id,),
            )
        ]
        assert reasons, task_id


async def test_the_executor_cannot_approve_even_as_a_human_named_agent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Имя в теле не делает агента человеком: признак — из токена."""
    headers = auth(monkeypatch)
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id, headers=headers["impl"])
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "denis", "expected_generation": 1},
        headers=headers["impl"],
    )
    assert resp.status_code in (403, 400, 409), resp.text
    assert dict(await repo.get_task(db, task_id))["status"] == "review"


# --- дети и создание --------------------------------------------------------


async def test_a_state_task_is_a_leaf_and_bulk_children_carry_the_kind(
    client: AsyncClient, db: aiosqlite.Connection
):
    parent = await client.post("/api/tasks", json={"title": "родитель"})
    created = await client.post(
        f"/api/tasks/{parent.json()['id']}/subtasks",
        json={
            "items": [{"title": "дочка", "result_kind": "state", "rollback": "откат"}],
            "task_type": "subtask",
            "source": "human",
        },
    )
    assert created.status_code == 200, created.text
    child = created.json()[0]
    assert child["result_kind"] == "state" and child["rollback"] == "откат"

    leaf = await make_state_task(db, title="лист")
    single = await client.post(
        "/api/tasks",
        json={"title": "ребёнок", "task_type": "subtask", "parent_id": leaf},
    )
    assert single.status_code == 422, single.text
    bulk = await client.post(
        f"/api/tasks/{leaf}/subtasks",
        json={"items": [{"title": "ещё"}], "task_type": "subtask", "source": "human"},
    )
    assert bulk.status_code == 422, bulk.text


async def test_a_task_with_children_cannot_become_a_state_task(
    client: AsyncClient, db: aiosqlite.Connection
):
    parent = (
        await client.post("/api/tasks", json={"title": "с детьми", "source": "agent"})
    ).json()
    await client.post(
        f"/api/tasks/{parent['id']}/subtasks",
        json={"items": [{"title": "дочка"}], "task_type": "subtask", "source": "agent"},
    )
    resp = await client.post(
        f"/api/tasks/{parent['id']}/refine", json={"result_kind": "state"}
    )
    assert resp.status_code == 422, resp.text


# --- автоодобрение и approve -------------------------------------------------


async def test_dor_auto_approval_never_opens_a_state_draft(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    from hub import config
    from hub.services.auto_approve import maybe_auto_approve

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    pid = await make_project(db, "st-auto", {"dor": "auto"})
    task_id = await make_state_task(db, project_id=pid, status="draft")
    await db.execute(
        "UPDATE tasks SET dor_passed=1, risk_class='R0' WHERE id=?", (task_id,)
    )
    twin = await make_state_task(
        db, project_id=pid, status="draft", result_kind="commit"
    )
    await db.execute(
        "UPDATE tasks SET dor_passed=1, risk_class='R0' WHERE id=?", (twin,)
    )
    await db.commit()
    assert not await maybe_auto_approve(db, task_id)
    assert dict(await repo.get_task(db, task_id))["status"] == "draft"
    assert await maybe_auto_approve(db, twin), "commit-близнец одобряется как раньше"


async def test_a_human_approves_a_state_draft_without_code_fields(
    client: AsyncClient, db: aiosqlite.Connection
):
    task_id = await make_state_task(db, status="draft")
    resp = await client.post(f"/api/tasks/{task_id}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


# --- pair-start и claim -----------------------------------------------------


async def test_claim_gives_no_worktree_hint_for_a_state_task(
    client: AsyncClient, db: aiosqlite.Connection
):
    state_id = await make_state_task(db)
    commit_id = await make_state_task(db, result_kind="commit")
    for task_id, expected in ((state_id, False), (commit_id, True)):
        resp = await client.post(
            f"/api/tasks/{task_id}/claim", json={"agent": "dev", "session_id": "s-1"}
        )
        assert resp.status_code == 200, resp.text
        assert bool(resp.json()["worktree_hint"]) is expected, task_id


# --- CLI --------------------------------------------------------------------


def test_cli_refuses_a_worktree_for_a_state_task():
    from hub import cli

    reason = cli._worktree_refusal({"id": 5, "result_kind": "state"}, {}, "/clone")
    assert "result_kind=state" in reason and "worktree" in reason


def test_cli_evidence_flag_refuses_bad_json_without_echoing_it(capsys):
    import argparse

    from hub import cli

    body: dict = {}
    assert not cli._put_evidence(body, argparse.Namespace(evidence=f"[{MARKER}"))
    assert MARKER not in capsys.readouterr().err
    assert cli._put_evidence(body, argparse.Namespace(evidence="[]")) and body == {
        "evidence": []
    }
    assert not cli._put_evidence({}, argparse.Namespace(evidence='{"a": 1}'))
    assert cli._put_evidence(body, argparse.Namespace(evidence=""))


def test_cli_parser_carries_the_new_flags():
    from hub import cli

    parser = cli.build_parser()
    ns = parser.parse_args(
        ["review-verdict", "7", "approved", "--expected-generation", "3"]
    )
    assert ns.expected_generation == 3
    ns = parser.parse_args(["submit-review", "7", "--evidence", "[]"])
    assert ns.evidence == "[]"
    ns = parser.parse_args(
        ["task", "--title", "t", "--result-kind", "state", "--rollback", "r"]
    )
    assert (ns.result_kind, ns.rollback) == ("state", "r")
    ns = parser.parse_args(["refine", "7", "--rollback", "r2"])
    assert ns.rollback == "r2"


# --- веб --------------------------------------------------------------------


async def test_the_web_form_carries_the_generation_and_completes_a_state_task(
    client: AsyncClient, db: aiosqlite.Connection
):
    task_id = await make_state_task(db)
    await drive_to_review(client, db, task_id)
    page = (await client.get(f"/tasks/{task_id}")).text
    assert 'name="expected_generation" value="1"' in page

    no_gen = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": ""},
        follow_redirects=False,
    )
    assert no_gen.status_code == 303 and "review_error=" in no_gen.headers["location"]
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    ok = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "", "expected_generation": "1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and "review_error" not in ok.headers["location"]
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"


async def test_a_commit_task_form_has_no_generation_field(
    client: AsyncClient, db: aiosqlite.Connection
):
    commit_id = await make_state_task(db, result_kind="commit")
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=1 WHERE id=?",
        (commit_id,),
    )
    await db.commit()
    page = (await client.get(f"/tasks/{commit_id}")).text
    assert 'name="expected_generation"' not in page


# --- логи -------------------------------------------------------------------


async def test_nothing_of_the_evidence_reaches_the_log(
    client: AsyncClient, db: aiosqlite.Connection, caplog
):
    # INFO — уровень прода (hub.app.basicConfig). DEBUG здесь не берётся:
    # aiosqlite на нём печатает параметры любого запроса, и это журнал
    # библиотеки, а не хаба.
    caplog.set_level(logging.INFO)
    task_id = await make_state_task(db)
    assert (await pair_start(client, task_id)).status_code == 200
    sent = await submit(client, task_id, evidence_for(observed=f"{MARKER} норма"))
    assert sent.status_code == 200
    assert MARKER not in caplog.text, "значения доказательств в лог не пишутся"


def test_a_missing_hub_error_is_an_http_exception_with_a_dict_detail():
    exc = state_evidence.EvidenceError(state_evidence.KIND_CREDENTIAL, ac_ids=["AC-1"])
    from hub.services.state_task import evidence_refusal

    refusal = evidence_refusal(exc)
    assert isinstance(refusal, HTTPException) and refusal.status_code == 422
    assert refusal.detail["kind"] == "secret"


# --- двери автоматики: каждая названа и проверена сама по себе -----------------


async def _state_in_review(
    db: aiosqlite.Connection, project_id: int | None = None
) -> int:
    """State-задача в review, поколение 1 — без пути через REST."""
    task_id = await make_state_task(db, project_id=project_id)
    await db.execute(
        "UPDATE tasks SET status='review', submission_generation=1, "
        "submission_model='claude-fable-5' WHERE id=?",
        (task_id,),
    )
    await db.commit()
    return task_id


async def _events_of(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)
    )
    return int(rows[0][0])


async def test_the_paid_start_of_a_judge_and_an_advisor_rechecks_the_task(
    db: aiosqlite.Connection, monkeypatch
):
    """Перепроверка перед оплачиваемым стартом: заказ мог лечь мимо order_run."""
    from unittest.mock import AsyncMock

    from hub import config
    from hub.integrations import cursor_cloud
    from hub.services import steward_shadow
    from hub.services.result_kind import AUTOMATION_REFUSAL

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    provider = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", provider)
    task_id = await _state_in_review(db)
    for kind in ("verdict", "advisor"):
        await db.execute(
            "INSERT INTO steward_runs (task_id, generation, kind, status, deadline_at) "
            "VALUES (?, 1, ?, 'open', datetime('now', '+30 minutes'))",
            (task_id, kind),
        )
    await db.commit()

    await steward_shadow.start_due_runs(db)

    rows = await db.execute_fetchall(
        "SELECT kind, status, closed_reason FROM steward_runs WHERE task_id=? ORDER BY kind",
        (task_id,),
    )
    assert [(r["kind"], r["status"]) for r in rows] == [
        ("advisor", "refused"),
        ("verdict", "refused"),
    ]
    assert all(AUTOMATION_REFUSAL in r["closed_reason"] for r in rows), [
        dict(r) for r in rows
    ]
    provider.assert_not_awaited()


async def test_steward_sweeps_leave_a_state_task_without_a_trace(
    db: aiosqlite.Connection, monkeypatch
):
    """Свипы стюарда не пишут по state-задаче ни заказа, ни отказа, ни отсрочки."""
    from hub import config
    from hub.services import steward_advisor, steward_dispatch

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    monkeypatch.setattr(config, "STEWARD_ADVISOR_MODELS", ("gpt-5.3-codex",))
    pid = await make_project(db, "st-steward", {"steward_shadow": True})
    task_id = await _state_in_review(db, project_id=pid)
    await db.execute(
        "INSERT INTO steward_judgements (task_id, generation, kind, submitted_verdict, "
        "verdict, contour) VALUES (?, 1, 'verdict', 'approve', 'approve', 2)",
        (task_id,),
    )
    await db.commit()
    before = await _events_of(db, task_id)

    assert await steward_dispatch.order_due_runs(db) == 0
    assert await steward_advisor.order_due_advisors(db) == 0
    assert not await steward_advisor._order_one(db, task_id, 2)

    assert await _events_of(db, task_id) == before, "свипы оставили след"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM steward_runs WHERE task_id=?", (task_id,)
    )


async def test_the_dor_judgement_of_a_state_statement_is_still_ordered(
    db: aiosqlite.Connection, monkeypatch
):
    """Суждение о ПОСТАНОВКЕ положено state-задаче: оно не про сдачу."""
    from hub import config
    from hub.services import steward_dispatch

    monkeypatch.setattr(config, "STEWARD_MODE", "shadow")
    task_id = await make_state_task(db, status="draft")
    await db.execute(
        "UPDATE tasks SET statement_fingerprint='abc' WHERE id=?", (task_id,)
    )
    await db.commit()
    order = await steward_dispatch.order_run(db, task_id, 0, steward_dispatch.KIND_DOR)
    assert order is not None and order["kind"] == "dor"


async def test_advisor_outcomes_and_stewards_application_skip_a_state_task(
    db: aiosqlite.Connection, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from hub.models import StewardJudgementSubmit
    from hub.config import TokenIdentity
    from hub.services import steward_advisor, steward_applied, steward_judgement
    from hub.services import steward_shadow
    from hub.services.result_kind import AUTOMATION_REFUSAL

    monkeypatch.setattr(steward_shadow, "effective_mode", AsyncMock(return_value="act"))
    pid = await make_project(db, "st-apply", {"verdict": "steward"})
    task_id = await _state_in_review(db, project_id=pid)
    await db.execute(
        "INSERT INTO steward_judgements (task_id, generation, kind, submitted_verdict, "
        "verdict, contour) VALUES (?, 1, 'verdict', 'approve', 'approve', 2)",
        (task_id,),
    )
    await db.commit()

    # применение исхода советника: чужой «received» не доводит до apply_self_approval
    applied = AsyncMock(return_value=None)
    monkeypatch.setattr(steward_applied, "apply_self_approval", applied)
    monkeypatch.setattr(
        steward_advisor,
        "advisor_state",
        AsyncMock(return_value=SimpleNamespace(state=steward_advisor.STATE_RECEIVED)),
    )
    assert await steward_advisor.apply_advisor_outcomes(db) == 0
    applied.assert_not_awaited()
    monkeypatch.undo()

    # применение суждения и самоодобрение
    with pytest.raises(HTTPException) as caught:
        await steward_applied.apply_judgement(db, task_id, 1)
    assert caught.value.detail == AUTOMATION_REFUSAL
    monkeypatch.setattr(steward_shadow, "effective_mode", AsyncMock(return_value="act"))
    assert await steward_applied.apply_self_approval(db, task_id, 1) is None
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    # запись суждения о сдаче; о постановке (dor) — не этим отказом
    steward = TokenIdentity("steward", "steward", principal_id=3)
    for kind in ("verdict", "advisor"):
        with pytest.raises(HTTPException) as caught:
            await steward_judgement.record_steward_judgement(
                db,
                task_id,
                StewardJudgementSubmit(generation=1, kind=kind, verdict="approve"),
                steward,
            )
        assert (
            caught.value.status_code == 409
            and caught.value.detail == AUTOMATION_REFUSAL
        )
    try:
        await steward_judgement.record_steward_judgement(
            db,
            task_id,
            StewardJudgementSubmit(generation=1, kind="dor", verdict="approve"),
            steward,
        )
    except HTTPException as exc:
        assert exc.detail != AUTOMATION_REFUSAL, "dor-суждение не про сдачу"


async def test_the_autopilot_does_not_even_read_a_state_task(
    db: aiosqlite.Connection, monkeypatch
):
    from unittest.mock import AsyncMock

    from hub.services import auto_verdict

    spy = AsyncMock()
    monkeypatch.setattr(auto_verdict, "autopilot_stance", spy)
    state_id = await _state_in_review(db)
    assert not await auto_verdict.maybe_auto_verdict(db, state_id)
    spy.assert_not_awaited()
    commit_id = await make_state_task(db, result_kind="commit")
    spy.return_value = type(
        "S",
        (),
        {"audit_alerts": (), "outcome": "refuse", "feed_note": "", "reason": ""},
    )()
    await auto_verdict.maybe_auto_verdict(db, commit_id)
    spy.assert_awaited_once()


async def test_the_headless_gate_and_every_dispatcher_refuse_a_state_task(
    db: aiosqlite.Connection,
):
    """dispatch_task и четыре диспетчера conveyor: ни одного обращения к job."""
    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    class _Counting(NoopDispatch):
        submitted = 0

        async def submit_task(self, *args, **kwargs):
            type(self).submitted += 1
            return {"job_id": "j"}

    plugins.dispatch = _Counting()
    task_id = await make_state_task(db)
    task = dict(await repo.get_task(db, task_id))
    with pytest.raises(HTTPException) as caught:
        await orchestration.dispatch_task(db, task_id, task)
    assert caught.value.status_code == 422
    await orchestration.dispatch_review(db, task)
    await orchestration.dispatch_fix(db, task, "правки")
    await orchestration.dispatch_arbiter(db, task, [])
    await orchestration.dispatch_ci_fix(db, task, {})
    assert _Counting.submitted == 0
    assert dict(await repo.get_task(db, task_id))["status"] == "open"
    assert dict(await repo.get_task(db, task_id))["job_id"] in (None, "")


async def test_the_post_done_transition_never_completes_a_state_task(
    db: aiosqlite.Connection,
):
    from hub.services import orchestration

    task_id = await make_state_task(db)
    await db.execute(
        "UPDATE tasks SET status='running', auto_review=0 WHERE id=?", (task_id,)
    )
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    assert (
        await orchestration.transition_after_agent_done(db, task, has_done=True)
        == "running"
    )
    assert dict(await repo.get_task(db, task_id))["status"] == "running"


async def test_rework_of_a_held_state_task_goes_back_to_open_without_a_job(
    client: AsyncClient, db: aiosqlite.Connection
):
    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins

    class _Counting(NoopDispatch):
        submitted = 0

        async def submit_task(self, *args, **kwargs):
            type(self).submitted += 1
            return {"job_id": "j"}

    plugins.dispatch = _Counting()
    task_id = await make_state_task(db)
    await db.execute(
        "UPDATE tasks SET status='needs_decision', claimed_by='dev', "
        "claim_session_id='s', submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await db.commit()
    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "rework", "instructions": "доделать"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open" and not resp.json()["job_id"]
    assert _Counting.submitted == 0, "headless-задание заказано для state"
    row = dict(await repo.get_task(db, task_id))
    assert not row["claimed_by"] and not row["job_id"]


# --- исполнитель: слой брони и выбор кандидата --------------------------------


async def test_the_reservation_itself_refuses_a_state_task(db: aiosqlite.Connection):
    from hub.services import executor_launch as el

    project_id = await make_project(db, "st-reserve")
    project = dict(await repo.get_project(db, project_id))
    state_id = await make_state_task(db)

    async def pick(_db, _project):
        return state_id, ""

    result = await el._reserve(db, project, None, "m", pick)
    assert isinstance(result, el.LaunchResult) and not result.launched
    assert el.REASON_STATE_TASK in result.reason
    assert not await db.execute_fetchall("SELECT 1 FROM executor_runs")


async def test_the_queue_hands_a_state_task_to_people_but_not_to_the_executor(
    db: aiosqlite.Connection,
):
    from hub.services import orchestrator_queue as oq

    pid = await make_project(db, "st-queue", {"orchestrator_queue": "shadow"})
    state_id = await make_state_task(db, project_id=pid)
    await db.execute(
        "UPDATE tasks SET dor_passed=1, priority='critical' WHERE id=?", (state_id,)
    )
    commit_id = await make_state_task(db, project_id=pid, result_kind="commit")
    await db.execute(
        "UPDATE tasks SET dor_passed=1, affected_areas=? WHERE id=?",
        (json.dumps(["docs/x.md"]), commit_id),
    )
    running = await make_state_task(db, project_id=pid, result_kind="commit")
    await db.execute(
        "UPDATE tasks SET status='running', affected_areas=? WHERE id=?",
        (json.dumps(["hub/y.py"]), running),
    )
    await db.commit()
    project = dict(await repo.get_project(db, pid))

    for_people = await oq.next_task(db, project)
    assert for_people["next_task_id"] == state_id, "state — не неизвестная область"
    for_executor = await oq.next_task(db, project, exclude_state=True)
    assert for_executor["next_task_id"] == commit_id
    assert state_id not in [c["task_id"] for c in for_executor["candidates"]]


async def test_the_statement_stays_editable_while_running_before_the_first_submission(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Заморозка — после СДАЧИ: пока поколения нет, правка в running допустима."""
    task_id = await make_state_task(db)
    assert (await pair_start(client, task_id)).status_code == 200
    refined = await client.post(
        f"/api/tasks/{task_id}/refine", json={"rollback": "уточнённый"}
    )
    assert refined.status_code == 200, refined.text
    added = await client.post(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json={
            "id": "AC-3",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "manual",
        },
    )
    assert added.status_code == 201, added.text
