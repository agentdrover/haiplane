"""Сдача без записи: submit-review?dry_run=1 (#1975).

Launcher k8 узнавал, что payload сдачи не сходится с правилами хаба, только на
настоящей сдаче: 422 приходил после заморозки ВМ. Режим dry_run проходит тот же
конвейер шагов и ничего не пишет. Каждый тест сравнивает ответ dry-run с ответом
настоящей сдачи и содержимое таблиц до и после; доказательство «ничего не
записано» — не счёт строк, а равенство содержимого.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.error
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from hub import cli, config
from hub import repository as repo
from hub.services import chat_pair as cp
from hub.services import executor_orders as orders
from hub.services import ci_trust, finding_outcome, lifecycle, orchestration
from tests import test_executor_orders as _eo
from tests import submit_dry_run_support as sup
from tests.cli_support import _run_main
from tests.state_support import make_state_task, pair_start
from tests.test_executor_orders import _claim_and_start, _launch_redeem, _task

# Фикстуры мира заказов k8 (pytest находит их по имени в модуле).
world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

POLICY = {"ci_before_submit": "require", "submission_contract": "require"}
PIPELINE = tuple(
    step.name
    for step in (
        *lifecycle.SUBMIT_STEPS_BEFORE_SAME_SHA_CHECK,
        *lifecycle.SUBMIT_STEPS_AFTER_SAME_SHA_CHECK,
    )
    if step.name != "delivery_pr"
)


async def _unchanged(db, before, label: str) -> None:
    assert sup.changed(before, await sup.snapshot(db)) == [], label


# --------------------------------------------------------------------------
# AC-1: тот же отказ, что у настоящей сдачи
# --------------------------------------------------------------------------


async def test_dry_run_refuses_like_real_submit(client: AsyncClient, db):
    w = sup.world()
    tid, tip = await sup.ready_task(db, "dr-refuse", POLICY)
    unknown = [{"finding_uid": sup.UID, "outcome": "fixed"}]

    async def both(label: str, payload: dict):
        before = await sup.snapshot(db)
        with sup.tripwires() as calls:
            dry = await sup.submit(client, tid, payload, dry=True)
            await _unchanged(db, before, label)
            real = await sup.submit(client, tid, payload, dry=False)
        assert dry.status_code in (409, 422), (label, dry.text)
        assert sup.same(dry, real), (label, dry.text, real.text)
        assert "maybe_dispatch_review" not in calls, label
        return dry

    no_ci = await both("нет отчёта CI", sup.body())
    assert no_ci.json()["detail"]["reason"] == "ci_before_submit_unproven"
    contract = await both("контракт", {"agent": "dev", "summary": "s"})
    two = await both(
        "два отказа", {"agent": "dev", "summary": "s", "finding_outcomes": unknown}
    )
    assert two.json() == contract.json(), "первым отказывает первый шаг конвейера"

    await sup.post_ci(client, w, tid, tip)
    typed = await both("uid числом", sup.body(finding_outcomes=[{"finding_uid": 7}]))
    assert any(e["loc"][-1] == "finding_uid" for e in typed.json()["detail"])
    await both("неизвестный uid", sup.body(finding_outcomes=unknown))


# --------------------------------------------------------------------------
# AC-3: настоящая сдача после dry-run — как без него
# --------------------------------------------------------------------------


async def _shape(db, tid: int) -> dict:
    await db.commit()
    task = dict(await repo.get_task(db, tid))

    async def rows(sql: str):
        return [tuple(r) for r in await (await db.execute(sql, (tid,))).fetchall()]

    return {
        "generation": task["submission_generation"],
        "status": task["status"],
        "reports": await rows(
            "SELECT trust, trust_reason FROM ci_run_reports WHERE task_id=?"
        ),
        "attempts": await rows(
            "SELECT trust, trust_reason, trust_final FROM ci_report_attempts "
            "WHERE task_id=?"
        ),
        "ac": await rows(
            "SELECT ac_id, status FROM ac_test_results WHERE task_id=? ORDER BY ac_id"
        ),
        "submissions": await rows("SELECT COUNT(*) FROM submissions WHERE task_id=?"),
        "feed": await rows(
            "SELECT kind, COUNT(*) FROM task_updates WHERE task_id=? "
            "GROUP BY kind ORDER BY kind"
        ),
    }


async def test_real_submit_after_dry_run_unchanged(client: AsyncClient, db):
    w = sup.world()
    a, tip_a = await sup.ready_task(db, "dr-after-a", POLICY)
    b, tip_b = await sup.ready_task(db, "dr-after-b", POLICY)
    for tid, tip in ((a, tip_a), (b, tip_b)):
        await sup.post_ci(client, w, tid, tip)
        await sup.pend(db, tid)  # проекцию должна записать именно сдача
    with sup.tripwires() as calls:
        dry = await sup.submit(client, a, sup.body(), dry=True)
        assert dry.status_code == 200, dry.text
        assert dry.json()["would_submit"] is True
        real_a = await sup.submit(client, a, sup.body(), dry=False)
        real_b = await sup.submit(client, b, sup.body(), dry=False)
    assert real_a.status_code == real_b.status_code == 200, real_a.text
    assert real_a.json()["submission_generation"] == 1
    shape_a, shape_b = await _shape(db, a), await _shape(db, b)
    assert shape_a["generation"] == 1 and shape_a["submissions"] == [(1,)]
    assert shape_a["reports"] == [("verified", "")], "проекция записана сдачей"
    assert shape_a["ac"] == [("AC-1", "pass"), ("AC-2", "pass")], "доказательства"
    assert shape_a == shape_b, "с предварительным dry-run и без него одно и то же"
    assert calls.count("maybe_dispatch_review") == 2, "по одному заказу на сдачу"


# --------------------------------------------------------------------------
# AC-4: повтор того же sha из review
# --------------------------------------------------------------------------


async def test_dry_run_same_sha_matches_real(client: AsyncClient, db, monkeypatch):
    w = sup.world()
    tid, tip = await sup.ready_task(db, "dr-same", POLICY)
    await sup.post_ci(client, w, tid, tip)
    with sup.tripwires() as calls:
        first = await sup.submit(client, tid, sup.body(), dry=False)
        assert first.status_code == 200, first.text
        findings = [
            {
                "title": t,
                "severity": "high",
                "category": "correctness",
                "locator": "file",
                "file": "hub/db.py",
            }
            for t in ("утечка курсора", "гонка записи")
        ]
        await repo.insert_machine_review(
            db,
            task_id=tid,
            submission_generation=1,
            harness_skill="lite-diff-review",
            raw_count=2,
            findings_confirmed=json.dumps(findings),
            unresolved="[]",
            incomplete=False,
        )
        await db.commit()
        uids = [
            f["finding_uid"] for f in await finding_outcome.open_findings(db, tid, 1)
        ]
        assert len(uids) == 2
        calls.clear()

        async def pair(label: str, payload: dict, status: int = 200):
            before = await sup.snapshot(db)
            dry = await sup.submit(client, tid, payload, dry=True)
            assert dry.status_code == status, (label, dry.text)
            await _unchanged(db, before, label)
            real = await sup.submit(client, tid, payload, dry=False)
            assert real.status_code == status, (label, real.text)
            return dry, real

        dry, real = await pair("(а) без исходов", sup.body())
        assert dry.json()["same_sha_noop"] is True
        assert real.json()["submission_generation"] == 1, "без перехода"

        dry, real = await pair(
            "(в) неизвестный uid",
            sup.body(finding_outcomes=[{"finding_uid": sup.UID, "outcome": "fixed"}]),
            422,
        )
        assert sup.same(dry, real)

        w.git.paths = [*sup.AREAS, "hub/extra.py"]
        areas = dict(await repo.get_task(db, tid))["affected_areas"]
        before = await sup.snapshot(db)
        dry = await sup.submit(client, tid, sup.body(accept_areas=True), dry=True)
        assert dry.status_code == 200, dry.text
        assert dry.json()["same_sha_noop"] is True
        assert {"resolve_diff", "surfaces"} <= set(dry.json()["passed_steps"])
        with monkeypatch.context() as off:
            off.setattr(config, "SDD_SURFACES", "off")
            off.setattr(config, "FINDING_OUTCOME", "off")
            quiet = await sup.submit(client, tid, sup.body(accept_areas=True), dry=True)
        steps = quiet.json()["passed_steps"]
        assert "resolve_diff" in steps and not {"surfaces", "finding_outcomes"} & set(
            steps
        )
        await _unchanged(db, before, "(г) область и риск не сохранены")
        real = await sup.submit(client, tid, sup.body(accept_areas=True), dry=False)
        assert real.status_code == 200
        grown = dict(await repo.get_task(db, tid))["affected_areas"]
        assert "hub/extra.py" in grown and grown != areas, "настоящая — сохраняет"

        answered = sup.body(
            finding_outcomes=[{"finding_uid": uids[0], "outcome": "fixed"}]
        )
        assert (await sup.submit(client, tid, answered, dry=False)).status_code == 200
        dry, real = await pair("(б) уже отвечен", answered)
        assert dry.json()["same_sha_noop"] is True
    assert "ensure_delivery_pr" not in calls and "pr_for_branch" not in calls


# --------------------------------------------------------------------------
# AC-5: CLI
# --------------------------------------------------------------------------


def test_cli_dry_run_flag(capsys):
    shown = {"dry_run": True, "would_submit": True, "same_sha_noop": False}
    shown |= {"passed_steps": ["task_is_submittable", "ci_before_submit"]}
    rc, api = _run_main(["submit-review", "5", "--dry-run"], api_result=shown)
    assert rc == 0
    assert api.call_args.args[:2] == ("POST", "/api/tasks/5/submit-review?dry_run=1")
    out = capsys.readouterr().out
    assert "would_submit" in out and "ci_before_submit" in out

    rc, api = _run_main(["submit-review", "5"], api_result={"id": 5})
    assert rc == 0 and api.call_args.args[1] == "/api/tasks/5/submit-review"

    detail = json.dumps({"detail": "finding_outcomes: неизвестный uid"})
    refused = urllib.error.HTTPError(
        "http://hub/x",
        422,
        "Unprocessable",
        {},
        io.BytesIO(detail.encode()),  # type: ignore[arg-type]
    )
    with (
        patch.object(sys, "argv", ["oc-hub", "submit-review", "5", "--dry-run"]),
        patch("urllib.request.urlopen", side_effect=refused),
    ):
        with pytest.raises(SystemExit) as stop:
            cli.main()
    assert stop.value.code != 0
    assert "HTTP 422" in capsys.readouterr().err


# --------------------------------------------------------------------------
# AC-6: задача-состояние
# --------------------------------------------------------------------------


async def test_dry_run_state_task_refused(client: AsyncClient, db):
    tid = await make_state_task(db)
    started = await pair_start(client, tid)
    assert started.status_code == 200, started.text
    before = await sup.snapshot(db)
    dry = await sup.submit(client, tid, {"agent": "dev"}, dry=True)
    assert dry.status_code == 422, dry.text
    assert dry.json()["detail"]["reason"] == "dry_run_unsupported_for_state"
    await _unchanged(db, before, "state-задача")


# --------------------------------------------------------------------------
# AC-7: форма ответа
# --------------------------------------------------------------------------


async def test_dry_run_response_model(client: AsyncClient, db, monkeypatch):
    from hub.services.submit_dry_run import SubmitDryRunView

    w = sup.world()
    path_rule = [{"pattern": "hub/**", "text": "перезапустить службу"}]
    full = {**POLICY, "path_notices": path_rule, "bug_red_test": "warn"}
    ok, tip = await sup.ready_task(db, "dr-model-ok", full)
    await sup.post_ci(client, w, ok, tip)
    warn, _ = await sup.ready_task(db, "dr-model-warn", {"ci_before_submit": "warn"})
    off, _ = await sup.ready_task(db, "dr-model-off", {"ci_before_submit": "off"})

    async def answer(tid: int) -> dict:
        resp = await sup.submit(client, tid, sup.body(), dry=True)
        assert resp.status_code == 200, resp.text
        view = SubmitDryRunView.model_validate(resp.json())
        assert view.dry_run is True and view.would_submit is True
        assert view.same_sha_noop is False and view.outcome_hint is None
        return resp.json()

    green = await answer(ok)
    assert tuple(green["passed_steps"]) == PIPELINE, "порядок конвейера"
    assert any("перезапустить службу" in n for n in green["notices"]), "path_notices"
    assert not any("CI до сдачи" in n for n in green["notices"])

    warned = await answer(warn)
    assert any("CI до сдачи не доказан" in n for n in warned["notices"])
    assert "ci_before_submit" in warned["passed_steps"]

    silent = await answer(off)
    assert "ci_before_submit" not in silent["passed_steps"], "шаг в режиме off"
    assert not any("CI до сдачи" in n for n in silent["notices"])

    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    assert "surfaces" not in (await answer(ok))["passed_steps"]

    spec = (await client.get("/openapi.json")).json()
    schema = spec["paths"]["/api/tasks/{task_id}/submit-review"]["post"]["responses"]
    refs = json.dumps(schema["200"])
    assert "TaskView" in refs and "SubmitDryRunView" in refs


# --------------------------------------------------------------------------
# AC-8: CI-проекция и expected_sha
# --------------------------------------------------------------------------


async def test_dry_run_ci_refresh_and_expected_sha_match_real(
    client: AsyncClient, db, monkeypatch
):
    w = sup.world()
    stale, tip_s = await sup.ready_task(db, "dr-ci-stale", POLICY)
    rerun, tip_r = await sup.ready_task(db, "dr-ci-rerun", POLICY)
    moved, tip_m = await sup.ready_task(db, "dr-ci-sha", POLICY)
    bug_policy = {**POLICY, "bug_red_test": "require"}
    bug, tip_b = await sup.ready_task(db, "dr-ci-bug", bug_policy, work_type="bug")
    red = {"state": "ran", "merge_base": "b" * 40}
    red["tests"] = {sup.REF_1: "failed", sup.REF_2: "failed"}
    await sup.post_ci(client, w, bug, tip_b, baseline=red)
    await sup.pend(db, bug)  # красный baseline виден только вычисленной проекции
    before = await sup.snapshot(db)
    with sup.tripwires():
        dry = await sup.submit(client, bug, sup.body(), dry=True)
        await _unchanged(db, before, "bug_red_test читает вычисленную проекцию")
        real = await sup.submit(client, bug, sup.body(), dry=False)
    assert dry.status_code == real.status_code == 200, (dry.text, real.text)
    for tid, tip in ((stale, tip_s), (rerun, tip_r)):
        await sup.post_ci(client, w, tid, tip)

    # (1) проекция pending, а GitHub уже подтверждает прогон.
    await sup.pend(db, stale)
    before = await sup.snapshot(db)
    with sup.tripwires():
        dry = await sup.submit(client, stale, sup.body(), dry=True)
        await _unchanged(db, before, "pending -> verified не сохранено")
        real = await sup.submit(client, stale, sup.body(), dry=False)
    assert dry.status_code == real.status_code == 200, (dry.text, real.text)
    assert dry.json()["would_submit"] is True

    # (2) старая зелёная проекция, а свежий прогон того же коммита красный.
    w.fake.attempt(rerun, tip_r, conclusion="failure", stamp=False)
    before = await sup.snapshot(db)
    with sup.tripwires():
        dry = await sup.submit(client, rerun, sup.body(), dry=True)
        await _unchanged(db, before, "красный rerun не сохранён")
        real = await sup.submit(client, rerun, sup.body(), dry=False)
    assert dry.status_code == 422 and sup.same(dry, real), (dry.text, real.text)

    # expected_sha: голова ушла (head_moved) и голову не увидели (head_unverified).
    for label, expected, fetch_ok in (
        ("head_moved", "b" * 40, True),
        ("head_unverified", tip_m, False),
    ):
        w.git.fetch_ok = fetch_ok
        payload = sup.body(expected_sha=expected)
        before = await sup.snapshot(db)
        with sup.tripwires():
            dry = await sup.submit(client, moved, payload, dry=True)
            await _unchanged(db, before, f"{label}: alert не записан")
            real = await sup.submit(client, moved, payload, dry=False)
        assert dry.status_code == 409 and sup.same(dry, real), (label, dry.text)
        assert dry.json()["detail"]["reason"] == label
        assert "task_updates" in sup.changed(before, await sup.snapshot(db)), (
            "настоящая сдача пишет alert — значит, проверка на dry-run что-то мерила"
        )

    # Чужой refresh держит CI-замок, проекция старая зелёная, дедлайн короткий:
    # настоящий refresh ничего не сохраняет и оставляет проекцию как есть.
    w.git.fetch_ok = True
    held, tip_h = await sup.ready_task(db, "dr-ci-lock", POLICY)
    await sup.post_ci(client, w, held, tip_h)
    monkeypatch.setattr(ci_trust, "REFRESH_DEADLINE_SECONDS", 0.05)
    lock = ci_trust._locks.setdefault((held, tip_h), asyncio.Lock())
    await lock.acquire()
    try:
        before = await sup.snapshot(db)
        with sup.tripwires():
            dry = await sup.submit(client, held, sup.body(), dry=True)
            await _unchanged(db, before, "замок занят: ничего не записано")
            real = await sup.submit(client, held, sup.body(), dry=False)
    finally:
        lock.release()
    assert dry.status_code == real.status_code == 200, (dry.text, real.text)
    assert dry.json()["would_submit"] is True

    # Пока preview ждёт замок, проекцию меняет другая корутина: dry-run видит
    # то, что в базе сейчас, как настоящий refresh, и замок не захватывает.
    class CountingLock(asyncio.Lock):
        acquired = 0

        async def acquire(self):
            type(self).acquired += 1
            return await super().acquire()

    flip, tip_f = await sup.ready_task(db, "dr-ci-flip", POLICY)
    await sup.post_ci(client, w, flip, tip_f)
    monkeypatch.setattr(ci_trust, "REFRESH_DEADLINE_SECONDS", 0.3)
    lock = CountingLock()
    ci_trust._locks[(flip, tip_f)] = lock
    await lock.acquire()
    CountingLock.acquired = 0
    try:
        with sup.tripwires():
            waiting = asyncio.ensure_future(
                sup.submit(client, flip, sup.body(), dry=True)
            )
            await asyncio.sleep(0.1)
            await db.execute(
                "UPDATE ci_run_reports SET trust='unverified', "
                "trust_reason='check_failed', attempt_key='' WHERE task_id=?",
                (flip,),
            )
            await db.commit()
            dry = await waiting
            assert CountingLock.acquired == 0, "dry-run замок не захватывал"
            real = await sup.submit(client, flip, sup.body(), dry=False)
    finally:
        lock.release()
    assert dry.status_code == 422 and sup.same(dry, real), (dry.text, real.text)


# --------------------------------------------------------------------------
# Мир заказов k8: заказ, прогон, слот и сессия ahcp_ у задачи (AC-2, AC-9)
# --------------------------------------------------------------------------

RULES = [{"pattern": "hub/**", "text": "перезапустить службу"}]


async def _slot_world(world, monkeypatch, *, start: bool) -> dict:
    """Задача на слоте k8 в running; git и контекст проекта — подставки."""
    task_id = await _task(world, "задача слота")
    launched = await _launch_redeem(world, task_id)
    if start:
        await _claim_and_start(world, task_id, launched["session"])
    else:
        await repo.update_task(world.db, task_id, status="running")
    gate = {"executor_launch": "manual", "ci_before_submit": "warn"}
    await repo.update_project(
        world.db, world.pid, gate_policy=json.dumps({**gate, "path_notices": RULES})
    )
    await world.db.commit()
    sup.world()
    context = {"repo": "/tmp/ws", "base_branch": "develop", "forge": "github"}
    monkeypatch.setattr(
        orchestration, "project_git_context", AsyncMock(return_value=context)
    )
    return {"id": task_id, "session": launched["session"], "order": launched["order"]}


async def test_dry_run_writes_nothing(world, monkeypatch):
    slot = await _slot_world(world, monkeypatch, start=True)
    tid, session, db = slot["id"], slot["session"], world.db
    prevention = {"kind": "accepted_risk", "reason": "r", "revisit": "v"}

    async def dry(payload, headers=None, status=200):
        before = await sup.snapshot(db)
        with sup.tripwires() as calls:
            resp = await sup.submit(
                world.client, tid, payload, dry=True, headers=headers or session
            )
        assert resp.status_code == status, resp.text
        assert calls == [], "delivery_pr и заказ ревью не вызывались"
        await _unchanged(db, before, str(payload)[:60])
        return resp.json()

    ok = await dry(sup.body(prevention=prevention))
    assert any("перезапустить" in n for n in ok["notices"]), "совпал path-notice"
    refused = sup.body(finding_outcomes=[{"finding_uid": sup.UID, "outcome": "fixed"}])
    await dry(refused, status=422)

    skip = ("executor_triage_state",)  # учёт самого sweep
    before = await sup.snapshot(db, skip)
    await orders.sweep_executor_orders(db)
    await orders.settle(db)
    assert sup.changed(before, await sup.snapshot(db, skip)) == [], "sweep не закрыл"
    me = await world.client.get("/api/whoami", headers=session)
    assert me.status_code == 200, "токен жив после sweep"
    assert (await _eo._order_row(world, slot["order"]["id"]))["state"] != "closed"
    await dry(sup.body())  # тот же ключ — снова 200

    human = {"headers": world.human_auth}
    with sup.tripwires():
        real = await sup.submit(world.client, tid, sup.body(), dry=False, **human)
    assert real.status_code == 200, real.text
    again = await dry(sup.body(), world.human_auth)
    assert again["same_sha_noop"] is True, "same-SHA без записей"

    findings = [{"title": "гонка", "severity": "high", "category": "correctness"}]
    findings[0] |= {"locator": "file", "file": "hub/db.py"}
    await repo.insert_machine_review(
        db,
        task_id=tid,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=1,
        findings_confirmed=json.dumps(findings),
        unresolved="[]",
        incomplete=False,
    )
    sent_back = await world.client.post(
        f"/api/tasks/{tid}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "чините",
            "findings": [{"id": 1, "severity": "high", "message": "см отчёт"}],
        },
        headers=world.human_auth,
    )
    assert sent_back.status_code == 200, sent_back.text
    uid = (await finding_outcome.open_findings(db, tid, 1))[0]["finding_uid"]
    deferred = [{"finding_uid": uid, "outcome": "deferred", "note": "позже"}]
    await dry(
        sup.body(finding_outcomes=deferred, prevention=prevention), world.human_auth
    )

    # Сравнение видит отзыв ключа, но не время его последнего использования.
    mark = await sup.snapshot(db)
    await db.execute("UPDATE api_keys SET last_used_at = '2000-01-01'")
    assert sup.changed(mark, await sup.snapshot(db)) == []
    await db.execute("UPDATE api_keys SET revoked_at = datetime('now')")
    assert sup.changed(mark, await sup.snapshot(db)) == ["api_keys"]


# --------------------------------------------------------------------------
# AC-9: кто вправе звать dry-run
# --------------------------------------------------------------------------


async def test_dry_run_access_matches_submit(world, monkeypatch):
    slot = await _slot_world(world, monkeypatch, start=False)
    tid, db = slot["id"], world.db
    other = await _task(world, "чужая задача")
    stranger = await _launch_redeem(world, other, "k9")
    code, _ttl = await cp.issue_code(db, world.human_id, kind="intake")
    intake = await _eo._redeem(world, code)
    assert intake.status_code == 200, intake.text
    intake_auth = {"Authorization": f"Bearer {intake.json()['token']}"}

    for label, headers in (
        ("сессия другой задачи", stranger["session"]),
        ("сессия intake", intake_auth),
    ):
        with sup.tripwires():
            dry = await sup.submit(
                world.client, tid, sup.body(), dry=True, headers=headers
            )
            real = await sup.submit(
                world.client, tid, sup.body(), dry=False, headers=headers
            )
        assert dry.status_code in (401, 403), (label, dry.text)
        assert sup.same(dry, real), label

    before = await sup.snapshot(db)
    with sup.tripwires():
        own = await sup.submit(
            world.client, tid, sup.body(), dry=True, headers=slot["session"]
        )
    assert own.status_code == 200, own.text
    after = await sup.snapshot(db)
    assert sup.changed(before, after) == ["chat_pair_sessions"], "только отметка токена"
    old = set(before["chat_pair_sessions"])
    assert len([r for r in after["chat_pair_sessions"] if r not in old]) == 1
