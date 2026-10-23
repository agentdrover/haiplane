from __future__ import annotations

import html
import re
from httpx import AsyncClient
from hub import repository as repo

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _web_project_tokens,
    _web_task_in_review,
    _web_task_in_review_with_test_ac,
)


async def test_review_panel_visible_for_client_driven_review(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Required" in resp.text
    assert f"/tasks/{task_id}/web-review-verdict" in resp.text
    assert 'name="verdict" value="approved"' in resp.text
    assert 'name="verdict" value="changes_requested"' in resp.text


async def test_task_page_shows_review_in_flight(client: AsyncClient, db):
    """AC-1 (#1027): the card names the in-flight dispatch, not just that a review is due."""
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    row = dict(await repo_module.get_task(db, task_id))
    await repo_module.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=int(row["submission_generation"] or 0),
        agent_id="bc-inflight",
        run_id="run-inflight",
        model="grok-4.6",
        profile="lite",
    )
    await db.commit()

    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert "ревью в полёте" in page.text
    assert "grok-4.6" in page.text
    assert "lite" in page.text
    assert "идёт" in page.text
    assert "grace" in page.text


async def test_review_panel_hidden_for_headless_review(client: AsyncClient, db):
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await repo_module.update_task(db, task_id, review_job_id="rev-42")
    await db.commit()

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Required" not in resp.text
    assert "In Progress" in resp.text


async def test_web_approve_verdict_returns_task_to_running(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "LGTM from the dashboard"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "running"
    assert body["review_approved_current"] is True
    assert body["latest_review"]["verdict"] == "approved"


async def test_web_changes_requested_with_findings(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": "see findings",
            "findings_text": "high: race in bump\njust a plain note\nlow: typo",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "running"
    assert body["review_cycle"] == 1
    findings = body["latest_review"]["findings"]
    assert [f["id"] for f in findings] == [1, 2, 3]
    assert findings[0]["severity"] == "high"
    assert findings[0]["message"] == "race in bump"
    assert findings[1]["severity"] == "medium"  # default for plain lines
    assert findings[2]["severity"] == "low"


async def test_task_detail_shows_finding_scope_and_linked_task(client: AsyncClient):
    # #435: out-of-scope findings render a scope badge and a link to the
    # follow-up task in the review panel.
    linked = await client.post("/api/tasks", json={"title": "Follow-up work"})
    linked_id = linked.json()["id"]
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix in this task"},
                {
                    "id": 2,
                    "severity": "low",
                    "message": "Handled separately",
                    "scope": "out_of_scope",
                    "linked_task_id": linked_id,
                },
            ],
        },
    )
    assert resp.status_code == 200

    # Resubmit so the task is back in client-driven review and the panel
    # (including the stale latest verdict with findings) is rendered.
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert "Fix in this task" in page.text
    assert "Handled separately" in page.text
    assert "out of scope" in page.text
    assert f'href="/tasks/{linked_id}"' in page.text
    assert f"#{linked_id}" in page.text


async def test_web_verdict_invalid_form_shows_error_not_500(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "maybe", "comments": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "review_error=" in resp.headers["location"]

    # Task untouched.
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review"
    assert body["review_verdict"] is None


async def test_web_solo_verdict_marked_and_badge_rendered(
    client: AsyncClient, monkeypatch
):
    """#434: a verdict accepted via HAIPLANE_REVIEW_SELF_APPROVE=allow is
    persisted as self-approved and badged next to the verdict in the panel."""
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    agent = {"Authorization": "Bearer agent-token"}

    # Created by the human (#360): an agent token cannot make ready work.
    resp = await client.post(
        "/api/tasks",
        json={"title": "Solo web"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "bot", "kind": "status", "content": "Plan: work"},
        headers=agent,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "session_id": "s-bot"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"

    # The implementer reviews their own work through the web panel.
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "changes_requested", "comments": "self check"},
        headers=agent,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}", headers=agent)).json()
    assert body["latest_review"]["self_approved"] is True

    # Back in review, the panel badges the solo verdict.
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"
    page = await client.get(f"/tasks/{task_id}", headers=agent)
    assert page.status_code == 200
    assert "badge-self-approved" in page.text


async def test_web_review_verdict_rejects_self_review(client: AsyncClient, monkeypatch):
    """#358 T6: the implementer cannot pass a verdict on their own work through
    the web panel.

    The protection already lives in the shared service, but until now it was
    asserted only through REST and MCP — self_review_forbidden appeared nowhere
    in this file. The two neighbours below cover the other branches: the solo
    opt-out (REVIEW_SELF_APPROVE=allow) and an independent reviewer.

    Verified by mutation: drop the raise from ensure_reviewer_independence and
    keep its self_approved return, and only this test fails — both neighbours
    stay green. So the refusal on the web route was genuinely unguarded, not
    merely covered somewhere less obvious.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    # The default, spelled out: this test is about the forbidding branch, and
    # a neighbour flipping the global would silently turn it into a no-op.
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    agent = {"Authorization": "Bearer agent-token"}

    # Created by the human (#360): an agent token cannot make ready work.
    resp = await client.post(
        "/api/tasks",
        json={"title": "Self review web"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "bot", "kind": "status", "content": "Plan: work"},
        headers=agent,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "session_id": "s-bot"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"

    # The implementer approves their own work through the web panel.
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "looks good to me"},
        headers=agent,
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "self_review_forbidden"

    # No verdict recorded, and the task is still waiting for a real reviewer.
    body = (await client.get(f"/api/tasks/{task_id}", headers=agent)).json()
    assert body["latest_review"] is None
    assert body["review_approved_current"] is False
    assert body["status"] == "review"


async def test_web_independent_verdict_has_no_solo_badge(client: AsyncClient):
    """#434: without the opt-out nothing changes — no badge, no mark."""
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "changes_requested", "comments": "rework"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["latest_review"]["self_approved"] is False

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.json()["status"] == "review"
    page = await client.get(f"/tasks/{task_id}")
    assert "badge-self-approved" not in page.text


async def test_review_panel_shows_machine_review(client: AsyncClient, db):
    # #381: summary and confirmed findings render above the verdict buttons.
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="MR panel task"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)

    resp = await client.post(
        f"/api/tasks/{tv.id}/machine-review",
        json={
            "raw_count": 5,
            "incomplete": False,
            "findings_confirmed": [
                {
                    "locator": "none",
                    "title": "missing test",
                    "severity": "low",
                    "category": "tests",
                }
            ],
            "findings_rejected": [{"title": "noise", "reason": "unreachable"}],
        },
    )
    assert resp.status_code == 200

    page = await client.get(f"/tasks/{tv.id}")
    assert "Machine review" in page.text
    assert "5 raw" in page.text
    assert "1 confirmed" in page.text
    assert "missing test" in page.text


async def test_review_panel_gap_warning_and_request_button(client: AsyncClient, db):
    # #382: warning + Request machine review button; button sets override.
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="Gap task"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await repo_module.update_task(db, tv.id, size="M", work_type="feature")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)

    page = await client.get(f"/tasks/{tv.id}")
    assert "machine-review отсутствует" in page.text
    assert "web-request-machine-review" in page.text

    resp = await client.post(
        f"/tasks/{tv.id}/web-request-machine-review", follow_redirects=False
    )
    assert resp.status_code == 303
    row = await repo_module.get_task(db, tv.id)
    assert row["machine_review_override"] == "require"
    events = [
        dict(e)
        for e in await repo_module.list_events(
            db, since=0, kinds=["machine_review_requested"]
        )
    ]
    assert events and events[0]["task_id"] == tv.id


async def test_the_base_merge_block_is_painted_where_approve_is_clicked(
    client: AsyncClient, db, monkeypatch
):
    """AC-1 доходит до КАРТОЧКИ, а не только до API брифа (#1233).

    Находка b3d276963b8303bf, и она настоящая. 09.09.2026 человек одобрил #1204
    именно на этой карточке и через четырнадцать секунд узнал из отказа
    доставки, что ветка конфликтует с базой в трёх файлах. Блок, посчитанный в
    брифе, но не нарисованный там, где нажимают Approve, не успевает ни к
    какому решению — а «до вердикта» и есть весь предмет AC-1.
    """
    from unittest.mock import AsyncMock

    from hub.services import review_brief

    task_id = await _web_task_in_review_with_test_ac(client, db)
    monkeypatch.setattr(
        review_brief,
        "base_merge_section",
        AsyncMock(
            return_value=review_brief.BaseMergeState(
                state="conflicting",
                reason="мерж в базу НЕ будет чистым",
                files=[
                    "tests/test_review_dispatch.py",
                    "hub/services/orchestration.py",
                ],
            )
        ),
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    assert "Мерж в базу:" in resp.text, (
        "расхождение с базой обязано быть видно на карточке до вердикта"
    )
    assert "conflicting" in resp.text
    assert "tests/test_review_dispatch.py" in resp.text, (
        "имена конфликтующих файлов названы человеку, а не только агенту"
    )
    assert resp.text.index("Мерж в базу:") < resp.text.rindex('value="approved"'), (
        "блок стоит ДО кнопки Approve: после неё вердикт уже потрачен"
    )


# ---------------------------------------------------------------------------
# #1333: принятие в интерфейсе умеет доставить.
#
# 23.09.2026 #1276, #1249 и #1234 приняты в веб-форме и закрыты со строкой
# «Судьба PR не выбрана»: web_decide_task не знал поля pr_disposition, и
# пустое значение было единственно возможным. Тесты читают форму так, как её
# отправит браузер, если человек ничего не трогал.
# ---------------------------------------------------------------------------


def _pr_choice_blocks(page: str) -> list[str]:
    return re.findall(r'<select name="pr_disposition".*?</select>', page, re.S)


def _preselected(block: str) -> str:
    chosen = re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', block)
    assert len(chosen) == 1, f"выбранным помечено {len(chosen)} вариантов: {block}"
    return chosen[0]


async def _decision_with_open_pr(client: AsyncClient, db, *, pr: int, approved: bool):
    from tests.test_accept_without_delivery import (
        _approved_task,
        _task_awaiting_decision,
    )

    if approved:
        return await _approved_task(client, db, title=f"web accept {pr}", pr=pr)
    return await _task_awaiting_decision(client, db, title=f"web accept {pr}", pr=pr)


async def test_web_accept_delivers_by_default(client: AsyncClient, db, monkeypatch):
    from tests.test_accept_without_delivery import _install, _MergeSpy, _pr_states

    spy = _MergeSpy()
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {1501: "open"})
    task_id = await _decision_with_open_pr(client, db, pr=1501, approved=True)

    page = (await client.get(f"/tasks/{task_id}")).text
    blocks = _pr_choice_blocks(page)
    # #1813: форма «Подробностей» и лист «Принять» несут один и тот же выбор.
    assert len(blocks) == 2, "выбор судьбы PR: форма решения и её лист"
    assert {_preselected(b) for b in blocks} == {"deliver"}
    shown = _preselected(blocks[0])
    assert shown == "deliver", "открытый PR и одобрение текущей сдачи — доставить"
    assert 'value="abandon"' in blocks[0], "отказаться — видимый выбор"

    # Браузер отправляет то, что показано выбранным.
    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={"action": "accept", "pr_disposition": shown},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert spy.merged == [1501], "принятие в интерфейсе доставило PR"
    assert await repo.pipeline_merge_recorded(db, task_id, 1501)
    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is None or stored["state"] != "pr_open"

    # Без одобрения текущей сдачи умолчание пустое: доставлять нечего решать.
    unapproved = await _decision_with_open_pr(client, db, pr=1502, approved=False)
    page = (await client.get(f"/tasks/{unapproved}")).text
    assert _preselected(_pr_choice_blocks(page)[0]) == ""


async def test_web_accept_can_abandon_the_pr(client: AsyncClient, db, monkeypatch):
    from tests.test_accept_without_delivery import _install, _MergeSpy, _pr_states

    spy = _MergeSpy()
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {1503: "open"})
    task_id = await _decision_with_open_pr(client, db, pr=1503, approved=True)

    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={"action": "accept", "pr_disposition": "abandon"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert (await repo.get_task(db, task_id))["status"] == "completed"
    assert spy.merged == [], "отказ от PR ничего не вливает"
    stored = await repo.get_delivery_discrepancy(db, task_id)
    assert stored is not None
    assert stored["disposition"] == "abandon"


async def test_every_web_decide_surface_carries_the_pr_choice(client: AsyncClient, db):
    """Поимённо: inbox, карточка канбана, строка таблицы — у каждой кнопки
    Accept свой селектор, и кнопка его отправляет (hx-include)."""
    approved = await _decision_with_open_pr(client, db, pr=1504, approved=True)
    bare = await _decision_with_open_pr(client, db, pr=1505, approved=False)

    surfaces = {
        "inbox": ("/", "inbox-pr-disposition"),
        "task_card": ("/partials/kanban", "card-pr-disposition"),
        "task_table": ("/tasks/list?status=needs_decision", "row-pr-disposition"),
    }
    for name, (url, prefix) in surfaces.items():
        page = (await client.get(url)).text
        for task_id, expected in ((approved, "deliver"), (bare, "")):
            field_id = f"{prefix}-{task_id}"
            found = re.search(
                rf'<select name="pr_disposition" id="{field_id}".*?</select>',
                page,
                re.S,
            )
            assert found, f"{name}: нет селектора судьбы PR у #{task_id}"
            assert _preselected(found.group(0)) == expected, name
            assert f'hx-include="#{field_id}"' in page, (
                f"{name}: кнопка Accept #{task_id} не отправляет выбор"
            )


async def test_the_inbox_registry_row_delivers_through_the_gate(
    client: AsyncClient, db, monkeypatch
):
    """#1333: строка «Completed, PR still open» в инбоксе несёт кнопку
    «доставить», и она идёт тем же путём, что REST; агенту — 403."""
    from hub import config
    from hub.config import TokenIdentity
    from tests.test_accept_without_delivery import _install, _MergeSpy, _pr_states

    spy = _MergeSpy()
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {1506: "open"})
    task_id = await _decision_with_open_pr(client, db, pr=1506, approved=True)
    await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})

    page = (await client.get("/")).text
    assert f'action="/tasks/{task_id}/web-deliver-undelivered"' in page

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"agent-token": TokenIdentity("bot", "agent", principal_id=7)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = await client.post(
        f"/tasks/{task_id}/web-deliver-undelivered",
        headers={"Authorization": "Bearer agent-token"},
        follow_redirects=False,
    )
    assert agent.status_code == 403
    assert spy.merged == []
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)

    resp = await client.post(
        f"/tasks/{task_id}/web-deliver-undelivered", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert spy.merged == [1506]


# ---- #1333, сдача 2: находки машинного ревью (отчёт #479) ----


async def _accepted_red_ci_row(client: AsyncClient, db, monkeypatch, *, pr: int):
    """Строка реестра pr_open, доставка которой упрётся в красный CI."""
    from tests.test_accept_without_delivery import _install, _MergeSpy, _pr_states

    spy = _MergeSpy(ci="failed")
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {pr: "open"})
    task_id = await _decision_with_open_pr(client, db, pr=pr, approved=True)
    await client.post(f"/api/tasks/{task_id}/decide", json={"action": "accept"})
    return task_id, spy


async def test_a_refused_registry_delivery_returns_to_the_inbox_with_the_reason(
    client: AsyncClient, db, monkeypatch
):
    """aa0b738677921a18: отказ гейта — ожидаемый исход, а не ошибка ввода.
    Человек остаётся на инбоксе и видит причину, а не сырой JSON."""
    task_id, spy = await _accepted_red_ci_row(client, db, monkeypatch, pr=1507)

    resp = await client.post(
        f"/tasks/{task_id}/web-deliver-undelivered",
        data={"return_project": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/?") and f"delivery_refused={task_id}" in location
    assert spy.merged == []

    page = (await client.get(location)).text
    banner = re.search(r'<div class="delivery-refusal"[^>]*>(.*?)</div>', page, re.S)
    assert banner, "на инбоксе нет строки с причиной отказа"
    assert f"#{task_id}" in banner.group(1) and "ci_" in banner.group(1)

    # HTMX-вариант: фрагмент с причиной на месте, код 200.
    htmx = await client.post(
        f"/tasks/{task_id}/web-deliver-undelivered",
        headers={"HX-Request": "true"},
    )
    assert htmx.status_code == 200, htmx.text
    assert "ci_" in htmx.text and "не выполнена" in htmx.text


async def test_the_accept_confirm_names_the_pr_fate(client: AsyncClient, db):
    """feb9db856b3d78d9: подтверждение называет судьбу PR на каждой кнопке."""
    approved = await _decision_with_open_pr(client, db, pr=1508, approved=True)
    bare = await _decision_with_open_pr(client, db, pr=1509, approved=False)
    surfaces = {
        "inbox": "/",
        "task_card": "/partials/kanban",
        "task_table": "/tasks/list?status=needs_decision",
    }
    for name, url in surfaces.items():
        page = (await client.get(url)).text
        for task_id, words in (
            (approved, f"Accept #{approved} и доставить PR #1508?"),
            (bare, "PR #1509 останется открытым"),
        ):
            button = re.search(
                rf'<button[^>]*hx-post="/tasks/{task_id}/web-decide".*?</button>',
                page,
                re.S,
            )
            assert button, f"{name}: нет кнопки Accept у #{task_id}"
            confirm = re.search(r'hx-confirm="([^"]*)"', button.group(0))
            assert confirm, f"{name}: Accept #{task_id} без подтверждения"
            assert words in html.unescape(confirm.group(1)), (name, confirm.group(1))
        # Смена выбора меняет и текст подтверждения.
        assert 'data-confirm="' in page and "onchange=" in page, name


async def test_the_htmx_accept_names_the_delivery_outcome(
    client: AsyncClient, db, monkeypatch
):
    """feb9db856b3d78d9: после ответа строка называет исход доставки, а
    секция «Completed, PR still open» обновляется без перезагрузки."""
    from tests.test_accept_without_delivery import _install, _MergeSpy, _pr_states

    spy = _MergeSpy(ci="failed")
    _install(monkeypatch, spy)
    _pr_states(monkeypatch, {1510: "open", 1511: "open"})
    red = await _decision_with_open_pr(client, db, pr=1510, approved=True)

    resp = await client.post(
        f"/tasks/{red}/web-decide",
        data={
            "action": "accept",
            "pr_disposition": "deliver",
            "from_inbox": "true",
            "inbox_project": "",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200, resp.text
    assert "PR #1510 НЕ доставлен" in resp.text and "ci_" in resp.text
    oob = re.search(
        r'<div id="inbox-undelivered" hx-swap-oob="true">(.*?)</div>\s*$',
        resp.text,
        re.S,
    )
    assert oob, "секция «Completed, PR still open» не обновлена"
    assert f"/tasks/{red}/web-deliver-undelivered" in oob.group(1)

    spy.ci = "passed"
    green = await _decision_with_open_pr(client, db, pr=1511, approved=True)
    resp = await client.post(
        f"/tasks/{green}/web-decide",
        data={"action": "accept", "pr_disposition": "deliver"},
        headers={"HX-Request": "true"},
    )
    assert "PR #1511 доставлен" in resp.text
    assert spy.merged == [1511]


async def test_web_decide_names_the_field_that_failed(client: AsyncClient, db):
    """40ac6a807ddad21b: ошибка валидации называет то поле, что не прошло."""
    task_id = await _decision_with_open_pr(client, db, pr=1512, approved=False)
    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={"action": "bogus", "pr_disposition": "deliver"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "action" in detail and "pr_disposition" not in detail, detail

    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={"action": "accept", "pr_disposition": "merge-it"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "pr_disposition" in resp.json()["detail"]


async def test_state_task_card_shows_evidence_and_posts_generation_with_verdict(
    client: AsyncClient, db, monkeypatch
):
    """AC-3 (#1648): карточка state-задачи в review — метка «результат:
    состояние», rollback и таблица доказательств поколения; нет «тесты 0 из 0»
    и строк про отсутствующий CI; форма несёт expected_generation, форма с
    устаревшим поколением получает 409 и ничего не записывает."""
    from tests.state_support import (
        ROLLBACK,
        GitSpy,
        drive_to_second_generation,
        make_state_task,
    )
    from hub.integrations.registry import plugins

    task_id = await make_state_task(db, title="Переключить DNS")
    await drive_to_second_generation(client, db, task_id)
    # Карточка state не вычисляет блоки кода: ни отчёт, ни дифф, ни доставку,
    # ни суждение стюарда — любое такое чтение здесь падает.
    from hub import web
    from hub.services import delivery_state, review_evidence, task_diff

    def _refuse(name):
        async def refuse(*_args, **_kwargs):
            raise AssertionError(f"карточка state вычислила блок кода: {name}")

        return refuse

    monkeypatch.setattr(review_evidence, "review_report", _refuse("review_report"))
    monkeypatch.setattr(task_diff, "submission_files", _refuse("submission_files"))
    monkeypatch.setattr(delivery_state, "delivery_state", _refuse("delivery_state"))
    monkeypatch.setattr(
        web, "_steward_recommendation", _refuse("_steward_recommendation")
    )
    original_git = plugins.git_ops
    spy = GitSpy()
    plugins.git_ops = spy
    try:
        page = (await client.get(f"/tasks/{task_id}")).text
    finally:
        plugins.git_ops = original_git

    text = html.unescape(page)
    assert "результат: состояние" in text
    block = text.split('id="state-evidence"', 1)[1]
    assert ROLLBACK in block.split("<table", 1)[0], "rollback в блоке принятия"
    assert "task-review-board--review" not in text, "секции машинного ревью нет"
    assert "повторное наблюдение, поколение 2" in text, "доказательство поколения 2"
    assert "отвечает 203.0.113.7" not in text, "поколение 1 на карточке не показано"
    assert "2026-10-09T12:00:00Z" in text
    for absent in (
        "Ревью за эту сдачу не проводилось",
        "Машинное ревью",
        "Request machine review",
        "0 из 0",
        "CI по коммиту",
        "CI по закреплённому коммиту",
        "нет отчёта",
    ):
        assert absent not in text, absent
    assert 'name="expected_generation" value="2"' in page
    assert spy.calls == [], f"карточка state ходила в git: {spy.calls}"

    # устаревшая форма: плоский POST — 303 с причиной на карточке, htmx — 409
    stale = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "", "expected_generation": "1"},
        follow_redirects=False,
    )
    assert stale.status_code == 303 and "review_error=" in stale.headers["location"]
    stale_htmx = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "", "expected_generation": "1"},
        headers={"HX-Request": "true"},
    )
    assert stale_htmx.status_code == 409, stale_htmx.text
    assert "поколен" in stale_htmx.text
    assert dict(await repo.get_task(db, task_id))["status"] == "review"

    ok = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "", "expected_generation": "2"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and "review_error" not in ok.headers["location"]
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"
