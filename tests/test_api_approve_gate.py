"""Tests for the DoR-gated approve endpoint (#40).

The approve gate enforces Definition of Ready: a task whose required DoR
checks are not satisfied cannot be approved unless ``force=true`` is
explicitly passed. Overrides are allowed but must leave an audit trail
(``alert`` update + activity log marker).
"""

from __future__ import annotations

from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers — mirror the contract from test_api_refine to stay consistent
# ---------------------------------------------------------------------------


async def _create_draft_task(client: AsyncClient, **overrides) -> dict:
    """Create a draft task via the agent-source path so status=='draft'."""
    body = {
        "title": "t",
        "source": "agent",
        "agent": "test",
        **overrides,
    }
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["status"] == "draft", task
    return task


async def _make_dor_ready(client: AsyncClient, task_id: int) -> None:
    """Fill in every required field for the default 'feature' work_type."""
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["a"],
            "validation_commands": ["uv run pytest"],
            "size": "S",
            "wip_tag": "feature_work",
            "affected_areas": ["hub/services/dor.py"],
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_approve_of_dor_ready_task_succeeds(client: AsyncClient):
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


# ---------------------------------------------------------------------------
# Gate rejects approval when DoR fails
# ---------------------------------------------------------------------------


async def test_approve_of_empty_draft_returns_422_with_structured_detail(
    client: AsyncClient,
):
    task = await _create_draft_task(client)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 422, resp.text

    detail = resp.json()["detail"]
    assert detail["error"] == "dor_failed"
    assert detail["task_id"] == task["id"]
    assert isinstance(detail["score"], int)
    assert "has_user_story" in detail["missing_required"]
    assert "has_acceptance_criteria" in detail["missing_required"]
    # Recommendations must mirror the readiness contract.
    fields = {rec["field"] for rec in detail["recommendations"]}
    assert {"user_story", "acceptance_criteria"} <= fields
    assert detail["hint"].startswith("pass force=true")

    # And the task must NOT have transitioned: it stays in draft.
    get_resp = await client.get(f"/api/tasks/{task['id']}")
    assert get_resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Force override: allowed, but audited
# ---------------------------------------------------------------------------


async def test_force_approve_overrides_gate_and_opens_task(client: AsyncClient):
    task = await _create_draft_task(client)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={"force": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


async def test_force_approve_leaves_alert_update_with_missing_fields(
    client: AsyncClient,
):
    task = await _create_draft_task(client)

    await client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"force": True, "comment": "we know what we're doing"},
    )

    updates = await client.get(f"/api/tasks/{task['id']}/updates")
    alerts = [u for u in updates.json() if u["kind"] == "alert"]
    assert len(alerts) == 1
    content = alerts[0]["content"]
    assert "DoR failed" in content
    assert "has_user_story" in content
    assert "force=true" in content
    assert "we know what we're doing" in content


async def test_force_approve_marks_activity_log(client: AsyncClient):
    task = await _create_draft_task(client)

    await client.post(f"/api/tasks/{task['id']}/approve", json={"force": True})

    activity = await client.get("/api/activity")
    approves = [a for a in activity.json() if a["kind"] == "task_approved"]
    assert approves, "expected a task_approved entry in activity"
    # The most recent one is ours.
    latest = approves[0]
    assert f"#{task['id']}" in latest["summary"]
    assert "force=true" in latest["summary"]
    assert "missing=" in latest["summary"]


# ---------------------------------------------------------------------------
# Unchanged legacy guardrails
# ---------------------------------------------------------------------------


async def test_approve_of_non_draft_task_still_returns_400(client: AsyncClient):
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    first = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert first.status_code == 200

    # Second approve should fail with 400 (status is no longer 'draft').
    second = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert second.status_code == 400
    assert "draft" in second.text


async def test_approve_missing_task_returns_404(client: AsyncClient):
    resp = await client.post("/api/tasks/99999/approve", json={})
    assert resp.status_code == 404


async def test_force_approve_when_dor_passes_still_records_override_alert(
    client: AsyncClient,
):
    """Regression for review I7: force=true must always leave a human-
    override audit trail, even if DoR happened to pass anyway. Otherwise
    a postmortem can't tell who deliberately bypassed the gate."""
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    resp = await client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"force": True, "comment": "I'm in a hurry"},
    )
    assert resp.status_code == 200, resp.text

    updates = await client.get(f"/api/tasks/{task['id']}/updates")
    alerts = [u for u in updates.json() if u["kind"] == "alert"]
    assert len(alerts) == 1
    assert "force=true" in alerts[0]["content"]
    assert "I'm in a hurry" in alerts[0]["content"]

    activity = await client.get("/api/activity")
    approves = [a for a in activity.json() if a["kind"] == "task_approved"]
    assert "(force=true)" in approves[0]["summary"]


async def test_concurrent_approve_409_when_status_changed_under_us(
    client: AsyncClient, monkeypatch
):
    """Regression for review I5: if status flips from 'draft' between the
    pre-check read and the conditional UPDATE, approve must return 409."""
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    from hub import repository as repo

    real_transition = repo.transition_status_if

    async def racing_transition(db, task_id, *, expected_from, new_status):
        # Simulate a concurrent writer that closes the window just before
        # our UPDATE lands, by mutating status to something else first.
        await db.execute("UPDATE tasks SET status='rejected' WHERE id=?", (task_id,))
        return await real_transition(
            db, task_id, expected_from=expected_from, new_status=new_status
        )

    monkeypatch.setattr(repo, "transition_status_if", racing_transition)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 409
    assert "no longer draft" in resp.text


async def test_approve_422_missing_required_excludes_optional_checks_for_work_type(
    client: AsyncClient,
):
    """Regression for review I1: 'has_user_story' is optional for bugs,
    so a draft bug task must NOT see it in missing_required even though
    the underlying check failed.
    """
    task = await _create_draft_task(client)
    # Switch the draft to work_type=bug; user_story is not required there.
    refine = await client.post(
        f"/api/tasks/{task['id']}/refine", json={"work_type": "bug"}
    )
    assert refine.status_code == 200, refine.text

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # 'has_user_story' is optional for bugs and must be filtered out.
    assert "has_user_story" not in detail["missing_required"]
    # 'has_problem_statement' IS required for bugs and must be present.
    assert "has_problem_statement" in detail["missing_required"]


# ---------------------------------------------------------------------------
# #1610: решение об одобрении опирается на DoR того снимка, что действует в
# момент перехода, а не на расчёт, сделанный до write-лока.
# ---------------------------------------------------------------------------


async def _other_connection_sql(dsn: str, sql: str, args: tuple = ()) -> None:
    """Правка БД вторым соединением - как конкурентный refine."""
    from hub.db import connect

    other = await connect(dsn)
    try:
        await other.execute(sql, args)
        await other.commit()
    finally:
        await other.close()


def _after_each_calc(monkeypatch, action, *, times: int | None = None) -> list[int]:
    """Вклинить ``action(task_id)`` сразу после расчёта DoR, до write-лока.

    Детерминированная инъекция гонки: точка между расчётом и переходом.
    ``times`` ограничивает число срабатываний (None - каждый расчёт).
    Возвращает счётчик расчётов.
    """
    from hub.services import recommendations

    real = recommendations.calculate_readiness_with_recommendations
    calls: list[int] = []

    async def calc(conn, task_id, **kw):
        report = await real(conn, task_id, **kw)
        calls.append(task_id)
        if times is None or len(calls) <= times:
            await action(task_id)
        return report

    monkeypatch.setattr(
        recommendations, "calculate_readiness_with_recommendations", calc
    )
    return calls


async def _noop(_task_id: int) -> None:
    return None


async def _alerts(client: AsyncClient, task_id: int) -> list[str]:
    updates = await client.get(f"/api/tasks/{task_id}/updates")
    return [u["content"] for u in updates.json() if u["kind"] == "alert"]


async def _status_of(client: AsyncClient, task_id: int) -> str:
    return (await client.get(f"/api/tasks/{task_id}")).json()["status"]


async def test_a_refine_during_approval_cannot_pass_a_stale_dor(
    client: AsyncClient, db_dsn, monkeypatch
):
    # AC-1 (а): AC удалены между расчётом DoR и локом - одобрение без force
    # не должно пройти по устаревшему "DoR пройден".
    task = await _create_draft_task(client)
    tid = task["id"]
    await _make_dor_ready(client, tid)

    async def drop_acs(task_id: int) -> None:
        await _other_connection_sql(
            db_dsn, "DELETE FROM acceptance_criteria WHERE task_id=?", (task_id,)
        )

    calls = _after_each_calc(monkeypatch, drop_acs, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dor_failed"
    assert "has_acceptance_criteria" in detail["missing_required"]
    assert len(calls) == 2, "после расхождения DoR пересчитан вне лока"
    assert await _status_of(client, tid) == "draft"
    assert not await _alerts(client, tid), "записей override нет"


async def test_a_refine_while_inputs_load_cannot_pass_a_stale_dor(
    client: AsyncClient, db_dsn, monkeypatch
):
    # AC-1 (б): то же изменение ВО ВРЕМЯ загрузки входов - между чтением
    # задачи и чтением AC. Снимок согласован (одна read-транзакция), поэтому
    # расчёт идёт по старому состоянию, а сверка под локом видит новое.
    from hub import repository as repo

    task = await _create_draft_task(client)
    tid = task["id"]
    await _make_dor_ready(client, tid)
    real = repo.list_acceptance_criteria
    fired: list[int] = []

    async def drop_then_read(conn, task_id):
        if not fired:
            fired.append(task_id)
            await _other_connection_sql(
                db_dsn, "DELETE FROM acceptance_criteria WHERE task_id=?", (task_id,)
            )
        return await real(conn, task_id)

    monkeypatch.setattr(repo, "list_acceptance_criteria", drop_then_read)
    calls = _after_each_calc(monkeypatch, _noop)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={})
    assert resp.status_code == 422, resp.text
    assert len(calls) == 2, "расчёт шёл по согласованному снимку и был повторён"
    assert "has_acceptance_criteria" in resp.json()["detail"]["missing_required"]
    assert await _status_of(client, tid) == "draft"
    assert not await _alerts(client, tid)


async def test_three_changed_snapshots_in_a_row_refuse_with_409(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    # AC-1: всего три попытки, затем 409 с причиной и без записей.
    task = await _create_draft_task(client)
    tid = task["id"]
    await _make_dor_ready(client, tid)
    counter = {"n": 0}

    async def keep_changing(task_id: int) -> None:
        counter["n"] += 1
        await _other_connection_sql(
            db_dsn,
            "UPDATE tasks SET business_value=? WHERE id=?",
            (f"bv-{counter['n']}", task_id),
        )

    calls = _after_each_calc(monkeypatch, keep_changing)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={"force": True})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "statement_changed_during_approval"
    assert len(calls) == 3
    assert await _status_of(client, tid) == "draft"
    assert not await _alerts(client, tid), "отказ не оставляет override"
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM events WHERE kind='task_approved' AND task_id=?",
        (tid,),
    )
    assert rows[0][0] == 0, "перехода нет"


async def test_a_second_attempt_with_a_matching_snapshot_approves(
    client: AsyncClient, db_dsn, monkeypatch
):
    # Расхождение один раз, затем снимок совпал: обычное одобрение.
    task = await _create_draft_task(client)
    tid = task["id"]
    await _make_dor_ready(client, tid)

    async def touch(task_id: int) -> None:
        await _other_connection_sql(
            db_dsn, "UPDATE tasks SET business_value='new' WHERE id=?", (task_id,)
        )

    calls = _after_each_calc(monkeypatch, touch, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert len(calls) == 2
    assert await _status_of(client, tid) == "open"


async def _partial_draft(client: AsyncClient) -> int:
    task = await _create_draft_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"work_type": "feature", "problem_statement": "ps"},
    )
    assert resp.status_code == 200, resp.text
    return task["id"]


async def test_the_override_names_the_dor_the_decision_was_made_on(
    client: AsyncClient, db_dsn, monkeypatch
):
    # AC-2: DoR не пройден; refine между расчётом и локом (а) чинит его,
    # (б) меняет список missing. Решение и override - по актуальному снимку.
    async def fix_everything(task_id: int) -> None:
        await _make_dor_ready(client, task_id)

    async def fill_user_story(task_id: int) -> None:
        await _other_connection_sql(
            db_dsn,
            "UPDATE tasks SET user_story='as a user, I want X so that Y' WHERE id=?",
            (task_id,),
        )

    # (а) чинит: без force - одобрено без override
    tid = await _partial_draft(client)
    _after_each_calc(monkeypatch, fix_everything, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert not await _alerts(client, tid)
    monkeypatch.undo()

    # (а) чинит: с force - "DoR was already passing" и suffix сохранены
    tid = await _partial_draft(client)
    _after_each_calc(monkeypatch, fix_everything, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={"force": True})
    assert resp.status_code == 200, resp.text
    alerts = await _alerts(client, tid)
    assert len(alerts) == 1
    assert "force=true requested (DoR was already passing)" in alerts[0]
    assert "missing" not in alerts[0]
    activity = await client.get("/api/activity")
    summary = [a for a in activity.json() if a["kind"] == "task_approved"][0]["summary"]
    assert "(force=true)" in summary and "missing=" not in summary
    monkeypatch.undo()

    # (б) список меняется, DoR всё ещё не проходит: без force - актуальный 422
    tid = await _partial_draft(client)
    _after_each_calc(monkeypatch, fill_user_story, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={})
    assert resp.status_code == 422, resp.text
    missing = resp.json()["detail"]["missing_required"]
    assert "has_user_story" not in missing and "has_business_value" in missing
    monkeypatch.undo()

    # (б) с force - override называет ровно актуальный missing
    tid = await _partial_draft(client)
    _after_each_calc(monkeypatch, fill_user_story, times=1)
    resp = await client.post(f"/api/tasks/{tid}/approve", json={"force": True})
    assert resp.status_code == 200, resp.text
    alerts = await _alerts(client, tid)
    assert len(alerts) == 1
    assert "has_business_value" in alerts[0]
    assert "has_user_story" not in alerts[0], "ложного missing не бывает"


async def test_the_dor_fingerprint_covers_every_dor_input(client: AsyncClient, db):
    # AC-3: каждое поле, которое читает approve-расчёт, входит в отпечаток
    # либо явно исключено с причиной. Новое поле в DoR без отпечатка роняет
    # страж: снимок содержит только отпечатываемые поля, и чтение чужого
    # поля - KeyError, а не молчаливо устаревший DoR.
    import dataclasses

    from hub import repository as repo
    from hub.services import recommendations
    from hub.services.dor_snapshot import (
        AC_FIELDS,
        EXCLUDED_INPUTS,
        PROJECT_FIELDS,
        TASK_FIELDS,
        load_dor_snapshot,
    )

    class Recording(dict):
        def __init__(self, data, sink: set[str]):
            super().__init__(data)
            self._sink = sink

        def __getitem__(self, key):
            self._sink.add(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            self._sink.add(key)
            return super().get(key, default)

    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])
    partial = await _partial_draft(client)
    seen_task: set[str] = set()
    seen_ac: set[str] = set()
    seen_project: set[str] = set()
    for tid in (task["id"], partial):
        snap = await load_dor_snapshot(db, tid)
        spy = dataclasses.replace(
            snap,
            task=Recording(snap.task, seen_task),
            acs=tuple(Recording(a, seen_ac) for a in snap.acs),
            project=Recording(snap.project, seen_project),
        )
        await recommendations.calculate_readiness_with_recommendations(
            db, tid, snapshot=spy
        )
    assert seen_task and seen_ac and seen_project, "страж что-то прочитал"
    assert seen_task <= set(TASK_FIELDS), seen_task - set(TASK_FIELDS)
    assert seen_ac <= set(AC_FIELDS), seen_ac - set(AC_FIELDS)
    assert seen_project <= set(PROJECT_FIELDS), seen_project - set(PROJECT_FIELDS)

    # исключения названы, у каждого причина
    assert {"git_tree", "filesystem"} <= set(EXCLUDED_INPUTS)
    assert all(reason.strip() for reason in EXCLUDED_INPUTS.values())

    # и каждое поле отпечатка действительно меняет отпечаток
    snap = await load_dor_snapshot(db, task["id"])
    base = snap.fingerprint()
    assert base == (await load_dor_snapshot(db, task["id"])).fingerprint()
    for name in TASK_FIELDS:
        changed = dataclasses.replace(snap, task={**snap.task, name: "~changed~"})
        assert changed.fingerprint() != base, name
    for name in AC_FIELDS:
        first = {**snap.acs[0], name: "~changed~"}
        changed = dataclasses.replace(snap, acs=(first, *snap.acs[1:]))
        assert changed.fingerprint() != base, name
    for name in PROJECT_FIELDS:
        changed = dataclasses.replace(snap, project={**snap.project, name: "~changed~"})
        assert changed.fingerprint() != base, name
    assert repo  # импорт нужен модулю целиком


async def test_batch_names_a_changed_statement_per_item(
    client: AsyncClient, db_dsn, monkeypatch
):
    # AC-5: без гонки ответ прежний; гонка на одном элементе - он в skipped
    # с причиной и текстом, остальные одобрены.
    ok = (await _create_draft_task(client))["id"]
    racy = (await _create_draft_task(client))["id"]
    other = (await _create_draft_task(client))["id"]
    for tid in (ok, racy, other):
        await _make_dor_ready(client, tid)

    # без гонки
    plain = await client.post(
        "/api/tasks/batch-approve", json={"task_ids": [ok], "comment": "c"}
    )
    assert plain.status_code == 200, plain.text
    assert plain.json() == {"approved": [ok], "skipped": []}

    counter = {"n": 0}

    async def race_on_racy(task_id: int) -> None:
        if task_id != racy:
            return
        counter["n"] += 1
        await _other_connection_sql(
            db_dsn,
            "UPDATE tasks SET business_value=? WHERE id=?",
            (f"bv-{counter['n']}", task_id),
        )

    _after_each_calc(monkeypatch, race_on_racy)
    resp = await client.post(
        "/api/tasks/batch-approve", json={"task_ids": [racy, other]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approved"] == [other]
    assert len(body["skipped"]) == 1
    skipped = body["skipped"][0]
    assert skipped["task_id"] == racy
    assert skipped["reason"] == "statement_changed_during_approval"
    assert skipped["detail"].strip(), "текст причины приезжает в detail"
    assert await _status_of(client, racy) == "draft"
