"""Hub-dispatched cross-model reviews (#757): core (split of the former single file, #1670)."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub.integrations import cursor_cloud
from hub.services.project_policy import review_dispatch_enabled
from hub.services.review_dispatch import (
    DEEP,
    LITE,
    pick_review_model,
    pick_review_profile,
    process_surface_reasons,
    _diff_lines,
    split_generated,
    sweep_review_dispatches,
)
from tests.review_dispatch_support import (
    _DIFF_506,
    _HARMLESS_DIFF,
    _any_dispatch_row,
    _dispatch_row,
    _DispatchRecorder,
    _docs_diff,
    _submitted,
    _wire,
)

_CODE_DIFF = "+++ b/hub/x.py\n+x = 1\n"


def test_pick_review_model_prefers_another_family(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    assert pick_review_model("claude-fable-5") == "grok-4.6"
    # #1036: the preference list is now the subscription's, so a grok
    # implementer lands on composer — still another family, and unlike
    # gpt-5.3-codex it is a model this account can actually launch.
    assert pick_review_model("grok-4.5") == "composer-2.5"
    assert pick_review_model("") == "grok-4.6"
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "gemini-3.1-pro")
    assert pick_review_model("claude-fable-5") == "gemini-3.1-pro"


async def test_clean_submit_dispatches_cloud_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#757): a submission in a verdict=auto project dispatches the
    # reviewer with the task branch and the hub MCP; without the policy —
    # no dispatch at all.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-dispatch")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["repo_url"] == "https://github.com/mrPDA/spike-repo"
    assert call["starting_ref"].startswith(f"task-{task_id}/")
    assert call["model_id"] == "grok-4.6", "claude implementer → grok reviewer"
    assert call["reviewer_token"] == "reviewer-token"
    assert call["hub_mcp_url"].endswith("/mcp")
    assert "не коммить" in call["prompt_text"]

    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-1"
    events = [
        dict(r)
        for r in await repo.list_events(
            db, since=0, kinds=["review_dispatched"], limit=10
        )
    ]
    assert events and events[0]["actor"] == "policy"
    assert json.loads(events[0]["payload"])["model"] == "grok-4.6"

    await _submitted(client, db, "spike-nopolicy", verdict_auto=False)
    assert len(recorder.calls) == 1, "no policy — no dispatch"


async def test_finished_run_without_report_alerts_once(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#757): terminal run, no report, grace expired → one loud alert
    # and status=failed; the second sweep stays quiet.
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "run-2"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-silent")
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 2_500_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert len(alerts) == 1
    assert not await repo.list_active_review_dispatches(db)
    closed = await _any_dispatch_row(db, task_id)
    assert closed["status"] == "failed"
    assert closed["provider_tokens"] == 2_500_000

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "отчёт НЕ сдан" in u["content"]]) == 1


async def test_failed_dispatch_emits_event_for_closed_task(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1027): a miss after the task left review is visible in the event feed."""
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-closed"}, "run": {"id": "run-closed"}}
    )
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-closed-task")
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await repo.update_task(db, task_id, status="running")
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    events = [
        dict(e)
        for e in await repo.list_events(
            db, since=0, kinds=["review_dispatch_failed"], limit=50
        )
        if e["task_id"] == task_id
    ]
    assert events, "the miss must reach the feed, not only the closed task's tape"


async def test_missing_usage_leaves_dispatch_tokens_null(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1026): the API did not answer → NULL on the dispatch, not 0.
    # A missing bill is unknown, never a free run (#549).
    recorder = _DispatchRecorder({"agent": {"id": "bc-unk"}, "run": {"id": "run-unk"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-unknown-bill")
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    await sweep_review_dispatches(db)
    closed = await _any_dispatch_row(db, task_id)
    assert closed["status"] == "failed"
    assert closed["provider_tokens"] is None


def _usage_review(**extra) -> dict:
    return {
        "harness_skill": "multi-agent-review",
        "harness_version": 8,
        "raw_count": 3,
        "findings_confirmed": [],
        "findings_rejected": [
            {"title": "x", "category": "correctness", "reason": "no"}
        ],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.6",
        **extra,
    }


async def test_missing_tokens_spent_is_not_a_mismatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1431): the cloud reviewer never reports tokens_spent. A missing
    # number is not a disagreement — no alert; the provider's bill still lands
    # on the report and the dispatch settles.
    recorder = _DispatchRecorder({"agent": {"id": "bc-n"}, "run": {"id": "run-n"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-usage-none")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_usage_review()
    )
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "расходится с данными провайдера" in u["content"]]
    stamped = dict(await repo.get_latest_machine_review(db, task_id))
    assert stamped["tokens_spent"] is None
    assert stamped["provider_tokens"] == 100_000
    assert (await _any_dispatch_row(db, task_id))["status"] == "done"


async def test_a_real_usage_mismatch_is_still_flagged_once(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#757), AC-2 (#1431): the report's tokens are cross-checked against
    # the provider's usage — a big gap is flagged to the audit once, with both
    # numbers; the dispatch settles as done either way.
    recorder = _DispatchRecorder({"agent": {"id": "bc-3"}, "run": {"id": "run-3"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-usage")

    review = {
        "harness_skill": "multi-agent-review",
        "harness_version": 8,
        "raw_count": 3,
        "findings_confirmed": [],
        "findings_rejected": [
            {"title": "x", "category": "correctness", "reason": "no"}
        ],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
        "model": "grok-4.6",
        "tokens_spent": 1000,
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=review)
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    flags = [u for u in updates if "расходится с данными провайдера" in u["content"]]
    assert len(flags) == 1
    assert "tokens_spent=1000" in flags[0]["content"]
    assert "Cursor usage=100000" in flags[0]["content"]
    assert not await repo.list_active_review_dispatches(db)


async def test_dispatch_failure_degrades_visibly(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#757): the API refused (beta broke / no key) — one alert, the
    # submission itself is untouched.
    recorder = _DispatchRecorder(None)
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-apifail")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review", "the submit must not suffer"
    alerts = [
        u["content"] for u in body["updates"] or [] if "НЕ вызвано" in u["content"]
    ]
    assert len(alerts) == 1
    assert not await repo.list_active_review_dispatches(db)


# --- Which setting is missing (#1083) ----------------------------------------
#
# Three independent preconditions guard the call, and they used to collapse
# into one message listing all three with "or". Two of them live in the
# process environment on the host, so the card could not say which one fired:
# telling "no API key" from "the project has no repo" took an ssh. The alert
# is the ONLY trace a failed dispatch leaves — best-effort means nothing else
# breaks — so it has to name what is actually missing, and nothing else.


async def _config_alerts(client: AsyncClient, task_id: int) -> list[str]:
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    return [
        u["content"]
        for u in body["updates"] or []
        if "не хватает конфигурации" in u["content"]
    ]


async def test_missing_cursor_key_is_named_in_the_alert(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1083): only the API key is missing → the alert names it, stays
    # silent about the two settings that are in place, and prints no value.
    recorder = _DispatchRecorder({"agent": {"id": "bc-k"}, "run": {"id": "run-k"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "CURSOR_API_KEY", "")

    task_id = await _submitted(client, db, "spike-nokey")

    alerts = await _config_alerts(client, task_id)
    assert len(alerts) == 1, "one record per submission, as before"
    # Whole text, not substrings. "No values, no parts, no lengths" is an
    # invariant about everything the message does NOT say, and a set of `in`
    # checks can only ever ban the strings someone thought to ban: a len() or
    # a prefix appended later would pass them all. Equality bans the rest by
    # construction. The expected string is spelled out here rather than
    # imported from the service — a test that builds its expectation from the
    # code under test agrees with that code by definition.
    assert alerts[0] == (
        "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
        "CURSOR_API_KEY (ключ Cursor API). Вердикт остаётся человеку (#757)."
    )
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review", "the submit must not suffer"
    assert not recorder.calls, "nothing to call the reviewer with"


async def test_alert_names_every_missing_setting(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1083): two of three missing → both named in ONE alert, and the
    # third — the one that is fine — is not. Naming only the first found
    # would send the operator back for a second round trip.
    recorder = _DispatchRecorder({"agent": {"id": "bc-2m"}, "run": {"id": "run-2m"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "")

    task_id = await _submitted(client, db, "spike-norepo", repo_name="")

    alerts = await _config_alerts(client, task_id)
    assert len(alerts) == 1
    assert alerts[0] == (
        "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
        "repo проекта (owner/name на его форже); "
        "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера). "
        "Вердикт остаётся человеку (#757)."
    )
    assert not recorder.calls


_ALERT_HEAD = "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
_ALERT_TAIL = " Вердикт остаётся человеку (#757)."

# Every combination of the three preconditions, with the WHOLE text the card
# must carry. Written out, not assembled from the labels: an expectation built
# by the same join the service uses would agree with any join the service
# grows, including a wrong one.
#
# Exhaustive on purpose. Two review rounds in a row found the same shape of
# hole — a combination nobody tested, where a short-circuit or a swap names
# the wrong setting and every test stays green. Picking one more pair would
# have invited a third round; seven rows leave no combination to find.
_MISSING_COMBINATIONS: tuple[tuple[bool, bool, bool, str], ...] = (
    # (key configured, repo set, token set) -> exact alert
    (False, True, True, "CURSOR_API_KEY (ключ Cursor API)."),
    (True, False, True, "repo проекта (owner/name на его форже)."),
    (True, True, False, "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера)."),
    (
        False,
        False,
        True,
        "CURSOR_API_KEY (ключ Cursor API); repo проекта (owner/name на его форже).",
    ),
    (
        False,
        True,
        False,
        "CURSOR_API_KEY (ключ Cursor API); CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера).",
    ),
    (
        True,
        False,
        False,
        "repo проекта (owner/name на его форже); "
        "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера).",
    ),
    (
        False,
        False,
        False,
        "CURSOR_API_KEY (ключ Cursor API); repo проекта (owner/name на его форже); "
        "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера).",
    ),
)


async def test_every_combination_names_exactly_what_is_missing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 и AC-2 (#1083) на всех семи сочетаниях сразу. Одно сочетание на
    # раунд ревью — это способ никогда не закончить: пропущенная пара пускает
    # и замыкание на первом условии, и перестановку ярлыков.
    recorder = _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "run-x"}})
    for index, (key_ok, repo_ok, token_ok, tail) in enumerate(_MISSING_COMBINATIONS):
        _wire(monkeypatch, recorder)
        if not key_ok:
            monkeypatch.setattr(config, "CURSOR_API_KEY", "")
        if not token_ok:
            monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "")
        task_id = await _submitted(
            client,
            db,
            f"spike-combo-{index}",
            repo_name="mrPDA/spike-repo" if repo_ok else "",
        )
        alerts = await _config_alerts(client, task_id)
        assert len(alerts) == 1, f"one record per submission, combination {index}"
        assert alerts[0] == _ALERT_HEAD + tail + _ALERT_TAIL, f"combination {index}"
    assert not recorder.calls, "no combination here has everything it needs"


async def test_no_config_alert_when_everything_is_configured(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1083): the healthy path stays silent. A diagnostic that also
    # fires when nothing is wrong is worse than the disjunction it replaced.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ok"}, "run": {"id": "run-ok"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-configured")

    assert await _config_alerts(client, task_id) == []
    assert len(recorder.calls) == 1, "the reviewer is called as before"


async def test_low_risk_task_gets_lite_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#807): ordinary low-class work is reviewed cheaply, and the
    # profile travels with the run instead of being inferred later.
    recorder = _DispatchRecorder({"agent": {"id": "bc-lite"}, "run": {"id": "r-lite"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-lite")

    prompt = recorder.calls[0]["prompt_text"]
    assert "ЛЁГКОЕ ревью" in prompt
    assert "multi-agent-review" not in prompt, "lite must not call the harness"
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


async def test_high_risk_task_gets_deep_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#807, #1415): a migration-class change and a declared security
    # risk each buy the expensive harness on their own. A declared high risk
    # of any other kind no longer does (#1415).
    recorder = _DispatchRecorder({"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}})
    _wire(monkeypatch, recorder)

    by_class = await _submitted(
        client, db, "spike-deep-class", areas=["hub/db.py"], diff=_CODE_DIFF
    )
    assert (await _dispatch_row(db, by_class))["profile"] == "deep"
    assert "multi-agent-review" in recorder.calls[0]["prompt_text"]

    by_risk = await _submitted(
        client,
        db,
        "spike-deep-risk",
        # #1415: only a security risk buys the harness now; a declared
        # technical high one stays with the class — see
        # test_declared_technical_high_risk_no_longer_buys_deep.
        risks=[{"kind": "security", "severity": "low", "description": "d"}],
    )
    assert (await _dispatch_row(db, by_risk))["profile"] == "deep", (
        "a declared security risk is what the expensive harness is for"
    )


async def test_unclassified_task_gets_deep_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#807): no class is not a low class. Otherwise never classifying
    # a task would be the cheapest way to skip the harness.
    recorder = _DispatchRecorder({"agent": {"id": "bc-unk"}, "run": {"id": "r-unk"}})
    _wire(monkeypatch, recorder)

    # No declared areas and an empty diff: the class stays uncomputed all the
    # way through the submit-time recalculation (#583/#762).
    task_id = await _submitted(
        client,
        db,
        "spike-unclassified",
        areas=[],
        clear_risk_class=True,
        diff=_CODE_DIFF,
    )
    row = dict(await repo.get_task(db, task_id))
    assert not row["risk_class"], "the fixture must leave the class uncomputed"

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    # And the same for a class the enum cannot read at all.
    # #820: the rule now answers with its reasons, and judges against a diff.
    assert pick_review_profile({"risk_class": "R99"}, _CODE_DIFF)[0] == "deep"
    assert pick_review_profile({"risk_class": "R0"}, _HARMLESS_DIFF)[0] == "lite"
    assert (
        pick_review_profile(
            {"risk_class": "R0", "machine_review_override": "require"}, _HARMLESS_DIFF
        )[0]
        == "deep"
    ), "a human who asked for machine review asked for the real thing"


async def test_lite_prompt_names_no_token_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#893): the prompt asks for BEHAVIOUR, not for a token count.
    # The ceiling used to be spelled out; measured against the provider's
    # bill, the same runs cost 777k-1.97M, so the number told the reviewer
    # nothing it could act on. "One pass over the diff" it can act on, and
    # the report can be checked against it.
    recorder = _DispatchRecorder({"agent": {"id": "bc-l"}, "run": {"id": "r-l"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 40000)

    await _submitted(client, db, "spike-no-ceiling")

    prompt = recorder.calls[0]["prompt_text"]
    assert "40000" not in prompt, "no token ceiling is quoted to the reviewer"
    assert "бюджет" not in prompt, "and it is not called a budget either"
    assert "ОДИН проход" in prompt
    assert "не исследуй репозиторий целиком" in prompt
    # Coverage honesty never rested on the number and must survive its removal.
    assert "incomplete=true" in prompt and "lost_dimensions" in prompt


async def test_self_reported_overspend_follows_the_recorded_decision(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#893): a self-reported spend above the old ceiling no longer
    # rewrites the run's completeness. #807 forced incomplete=true here; in
    # eleven measured runs it never fired, because the reported number missed
    # the provider's bill by 12-62x — a run billed 1.5M declared 36k and
    # passed. The hub keeps the report as submitted and says nothing it
    # cannot know.
    recorder = _DispatchRecorder({"agent": {"id": "bc-b"}, "run": {"id": "r-b"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 1000)

    task_id = await _submitted(client, db, "spike-budget")

    body = {
        "harness_skill": "lite-diff-review",
        "agent_count": 1,
        "tokens_spent": 1_500_000,
        "raw_count": 1,
        "findings_confirmed": [
            {
                "locator": "file",
                "title": "off-by-one",
                "severity": "medium",
                "file": "a.py",
            }
        ],
        "findings_rejected": [],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor-cloud-reviewer",
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["profile"] == "lite", "the profile still comes from the dispatch"
    assert saved["incomplete"] == 0, "the hub does not rewrite what it cannot see"

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    assert not [
        u
        for u in data["updates"] or []
        if u["kind"] == "alert" and "бюджет" in u["content"]
    ], "no alert about a ceiling that bounds nothing"


async def test_declared_incomplete_still_stands(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The half of #807 AC-4 that was never in doubt: when the REVIEWER says it
    # did not read everything, that is a fact about coverage and it is kept.
    # Removing the budget guard must not quietly take this with it.
    recorder = _DispatchRecorder({"agent": {"id": "bc-i"}, "run": {"id": "r-i"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-declared-incomplete")

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 12_000,
            "raw_count": 0,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": True,
            "unresolved": [],
            "lost_dimensions": ["hub/app.py не прочитан"],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["incomplete"] == 1


async def test_report_without_dispatch_has_no_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "We do not know how this was reviewed" and "it was reviewed cheaply"
    # are different facts, and the cheap one must never be assumed.
    recorder = _DispatchRecorder({"agent": {"id": ""}, "run": {}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-no-dispatch", verdict_auto=False)

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "harness_version": 8,
            "agent_count": 4,
            "tokens_spent": 999999,
            "raw_count": 2,
            "findings_confirmed": [],
            "findings_rejected": [
                {"title": "noise", "category": "style", "reason": "not a defect"}
            ],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "dev",
        },
    )
    assert resp.status_code == 200, resp.text
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["profile"] == ""
    assert saved["incomplete"] == 0, "no dispatch — no budget rule to apply"


# --- The review key, separate from the verdict key (#805) --------------------
#
# "Call a reviewer" and "who signs the verdict" were one switch, which left
# the hub's own project choosing between no review and no human. They are
# two questions now, and only the first one spends tokens.


async def test_dispatch_runs_without_auto_verdict(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#805): review=dispatch calls the reviewer; the verdict stays
    # human — no auto-verdict is recorded for the submission.
    recorder = _DispatchRecorder({"agent": {"id": "bc-rev"}, "run": {"id": "r-rev"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-review-only", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "the reviewer must be called"
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "the task waits for a human verdict"
    assert not row["review_verdict"], "policy must not sign the verdict here"


async def test_verdict_auto_still_dispatches(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#805): projects that already run on verdict=auto keep working
    # without anyone editing their stored policy — the autopilot reads the
    # report, so asking for it implies asking for the review.
    recorder = _DispatchRecorder({"agent": {"id": "bc-auto"}, "run": {"id": "r-auto"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-legacy-auto", policy={"verdict": "auto"})

    assert len(recorder.calls) == 1


async def test_absent_review_policy_never_dispatches(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#805): today's behaviour for a project that asked for nothing.
    recorder = _DispatchRecorder({"agent": {"id": "bc-no"}, "run": {"id": "r-no"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-silent", policy={"dor": "human"})

    assert recorder.calls == []


async def test_unknown_review_value_falls_back_to_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#805): a typo, or a value from a future version, must not spend
    # tokens. Unreadable policy never grants anything — including budget.
    recorder = _DispatchRecorder({"agent": {"id": "bc-typo"}, "run": {"id": "r-typo"}})
    _wire(monkeypatch, recorder)

    await _submitted(client, db, "spike-typo", policy={"review": "dispath"})

    assert recorder.calls == []
    assert review_dispatch_enabled({"review": "dispatch"}) is True
    assert review_dispatch_enabled({"review": "off"}) is False
    assert review_dispatch_enabled({}) is False
    assert review_dispatch_enabled({"verdict": "auto"}) is True


_DIFF_509 = (
    "+++ b/hub/services/validation_run.py\n"
    "+            out, dropped = await asyncio.wait_for("
    "_collect(proc), timeout=_RUN_TIMEOUT)\n"
    "+        except TimeoutError:\n"
    "+            proc.kill()\n"
)


async def test_subprocess_surface_forces_deep_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#820): a low-class diff that starts a subprocess still buys deep,
    # and the feed says which surface bought it — "deep" alone cannot be
    # argued with later.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ps"}, "run": {"id": "r-ps"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-subprocess", diff=_DIFF_506)

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    notes = [
        u["content"] for u in data["updates"] or [] if "профиль deep" in u["content"]
    ]
    assert notes, "the dispatch note must name the profile"
    assert "процессная поверхность" in notes[0]
    assert "create_subprocess_exec" in notes[0]


async def test_workspace_surface_forces_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#820): touching the workspace or branch state is the other half
    # of the measured blind spot — #506's collection ran against whatever the
    # shared clone happened to be on.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ws"}, "run": {"id": "r-ws"}})
    _wire(monkeypatch, recorder)

    diff = (
        "+++ b/hub/services/thing.py\n"
        "+    await plugins.git_ops.checkout(workspace_path, branch)\n"
    )
    task_id = await _submitted(client, db, "spike-workspace", diff=diff)

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"


async def test_ordinary_diff_stays_lite(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#820): the saving #807 exists for must survive. Ordinary work,
    # and the very words in a COMMENT, keep the cheap profile — otherwise
    # 'deep' becomes the default by way of prose.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ord"}, "run": {"id": "r-ord"}})
    _wire(monkeypatch, recorder)

    talky = (
        "+++ b/hub/services/thing.py\n"
        "+# раньше здесь был create_subprocess_exec и wait_for(, теперь нет\n"
        '+    """Документация упоминает worktree и checkout, но кода нет."""\n'
        "+    return sorted(items)  # никаких подпроцессов\n"
    )
    task_id = await _submitted(client, db, "spike-ordinary", diff=talky)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite", (
        "markers inside comments and docstrings are talk, not code"
    )


async def test_unreadable_diff_forces_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#820): a diff nobody could read is not a harmless one. Same rule
    # the ladder uses for an uncomputed class (#582).
    recorder = _DispatchRecorder({"agent": {"id": "bc-nod"}, "run": {"id": "r-nod"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-nodiff", diff="")
    # An empty string is a readable, empty diff; None is the unreadable one.
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"

    profile, reasons = pick_review_profile({"risk_class": "R0"}, None)
    assert profile == "deep"
    assert reasons == ["дифф сдачи прочитать не удалось"]


def test_known_process_defect_diffs_would_get_deep():
    # AC-5 (#820): the regression standard is the real thing — the two diffs
    # whose process defects the cheap profile actually missed. If the rule
    # stops catching these, it has stopped being worth its cost.
    for name, diff in (("#506", _DIFF_506), ("#509", _DIFF_509)):
        profile, reasons = pick_review_profile({"risk_class": "R2"}, diff)
        assert profile == "deep", f"{name} must buy the expensive profile"
        assert any("процессная поверхность" in r for r in reasons), name


# --- The KIND of risk decides, not the word "high" (#827) --------------------
#
# From the first live dispatch (#818): a task honestly declaring "the daily
# message turns into noise" bought a multi-agent harness that cannot judge
# whether a message is noise. Dogfooding answers that question; a code review
# does not. The honest statement should not be the expensive one.

_PRODUCT_RISK = [
    {
        "kind": "other",
        "severity": "high",
        "description": "ежедневное сообщение превращается в шум",
    }
]


# Real `git log -p -U0 --cc` output (#1415, finding 7efb59b1): the author
# resolved a binary conflict in a merge and edited a document. The combined
# diff names the binary ONLY in its `diff --cc` line — no ---/+++ headers.
_CC_BINARY_AND_DOCS = (
    "diff --git a/docs/notes.md b/docs/notes.md\n"
    "index 7898192..422c2b7 100644\n"
    "--- a/docs/notes.md\n"
    "+++ b/docs/notes.md\n"
    "@@ -1,0 +2 @@ a\n"
    "+b\n"
    "diff --cc x.wasm\n"
    "index e73ddff,3e29efb..c5ff1d1\n"
    "Binary files differ\n"
)


def test_combined_diff_path_is_not_lost():
    # 7efb59b1: a path named only by `diff --cc` / `diff --combined` counts.
    task = {"risk_class": "R3", "risks": "[]"}
    profile, reasons = pick_review_profile(task, _CC_BINARY_AND_DOCS)
    assert profile == DEEP, reasons
    combined = _CC_BINARY_AND_DOCS.replace("diff --cc ", "diff --combined ")
    assert pick_review_profile(task, combined)[0] == DEEP
    # A file block whose path cannot be read is not documentation either.
    unreadable = _docs_diff("docs/x.md") + "diff --git garbage\nBinary files differ\n"
    assert pick_review_profile(task, unreadable)[0] == DEEP
    # And a `diff` header of a shape nobody taught the parser is unknown too.
    unknown = _docs_diff("docs/x.md") + "diff --tree x.wasm\nBinary files differ\n"
    assert pick_review_profile(task, unknown)[0] == DEEP


def test_hunk_body_is_not_read_as_a_header():
    # 44f0ab2a: real `git diff -U0` deleting the line "-- verbose" from a
    # document prints "--- verbose" INSIDE the hunk. That is text, not a file.
    diff = (
        "diff --git a/docs/x.md b/docs/x.md\n"
        "index 02dbddc..587be6b 100644\n"
        "--- a/docs/x.md\n"
        "+++ b/docs/x.md\n"
        "@@ -2 +1,0 @@ x\n"
        "--- verbose\n"
        "@@ -5,0 +5 @@ y\n"
        "+++ added\n"
    )
    task = {"risk_class": "R3", "risks": "[]"}
    assert pick_review_profile(task, diff) == (LITE, ["дифф только из документации"])


def test_quoted_paths_are_decoded():
    # ab56be42: with core.quotepath (the default) git prints non-ASCII names
    # as quoted octal. Real header for docs/заметки.md:
    q = '"a/docs/\\320\\267\\320\\260\\320\\274\\320\\265\\321\\202\\320\\272\\320\\270.md"'
    qb = '"b/' + q[3:]
    diff = (
        f"diff --git {q} {qb}\n"
        "index 02dbddc..587be6b 100644\n"
        f"--- {q}\n"
        f"+++ {qb}\n"
        "@@ -2 +1,0 @@ x\n"
        "-строка\n"
    )
    task = {"risk_class": "R3", "risks": "[]"}
    assert pick_review_profile(task, diff)[0] == LITE
    # Decoding must not launder code: a quoted path under hub/ stays code.
    code = diff.replace("docs/", "hub/").replace(".md", ".py")
    assert pick_review_profile(task, code)[0] == DEEP


async def test_product_high_risk_does_not_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#827): the real #818 risk, verbatim. It stays on the class.
    recorder = _DispatchRecorder({"agent": {"id": "bc-pr"}, "run": {"id": "r-pr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-product-risk", risks=_PRODUCT_RISK)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


def test_declared_technical_high_risk_no_longer_buys_deep():
    # AC-1 (#1415): a declared technical high risk, and a high risk of a kind
    # nobody recognises, no longer buy the harness. Measured 23.09 over 328
    # reports: deep bought by a declared risk is the weakest reason there is,
    # 0.54 confirmed findings per report. The class and the process surfaces
    # still decide, exactly as before.
    for kind in ("data_migration", "что-тонеизвестное"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R1",
                "risks": json.dumps([{"kind": kind, "severity": "high"}]),
            },
            _CODE_DIFF,
        )
        assert profile == LITE, (kind, reasons)
        assert reasons == ["класс риска R1, процессных поверхностей нет"], reasons


def test_security_risk_still_buys_deep():
    # AC-2 (#1415): security keeps the harness at any severity, on code and on
    # a documentation-only diff alike.
    for diff in (_CODE_DIFF, "+++ b/docs/spikes/x.md\n+текст\n"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R1",
                "risks": json.dumps([{"kind": "security", "severity": "low"}]),
            },
            diff,
        )
        assert profile == DEEP, diff
        assert reasons == ["заявлен риск security"], reasons


def test_docs_only_diff_gets_lite_mixed_diff_keeps_rule():
    # AC-3 (#1415): a diff of documentation alone has no executable code for
    # the harness to read — spike #1402 bought deep order 430 for one document.
    # One non-documentation file keeps the old rule.
    task = {"risk_class": "R3", "risks": "[]"}
    profile, reasons = pick_review_profile(task, _docs_diff("docs/spikes/x.md"))
    assert profile == LITE
    assert reasons == ["дифф только из документации"], reasons

    profile, reasons = pick_review_profile(task, _docs_diff("docs/x.md", "hub/x.py"))
    assert profile == DEEP
    assert reasons == ["класс риска R3"], reasons


def test_documentation_is_an_explicit_list():
    # #1415 constraint: tests, configs, templates and workflows are NOT
    # documentation, and a deleted or renamed code file is a code change.
    task = {"risk_class": "R3", "risks": "[]"}
    not_docs = (
        "hub/cli_templates/work_types/feature.md",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
        "tests/fixtures/x.md",
        "docs/agent-context/complexity-budget.json",
        "pyproject.toml",
        "skills/hub-testing/SKILL.md",
        "agents/code-reviewer.md",
        ".claude/steward-handoff.md",
    )
    for path in not_docs:
        profile, _ = pick_review_profile(task, _docs_diff("docs/x.md", path))
        assert profile == DEEP, path
    deleted = (
        "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ /dev/null\n-x = 1\n"
    )
    profile, _ = pick_review_profile(task, _docs_diff("docs/x.md") + deleted)
    assert profile == DEEP, "a deleted code file is not documentation"
    renamed = "diff --git a/hub/x.py b/docs/x.md\nrename from hub/x.py\n"
    profile, _ = pick_review_profile(task, renamed)
    assert profile == DEEP, "code renamed into docs/ is still a code change"
    for path in ("AGENTS.md", "CLAUDE.md", "docs/AGENTS.md", "sub/CLAUDE.md"):
        # Instruction files an agent loads are behaviour, not prose.
        profile, _ = pick_review_profile(task, _docs_diff(path))
        assert profile == DEEP, path
    for path in ("README.md", "deploy/CD.md", "docs/a/b.rst", "CONTRIBUTING.md"):
        profile, _ = pick_review_profile(task, _docs_diff(path))
        assert profile == LITE, path
    # An empty diff names no files: nothing proves it is documentation.
    assert pick_review_profile(task, "")[0] == DEEP
    # A human request still buys deep on a document.
    task_req = dict(task, machine_review_override="require")
    assert pick_review_profile(task_req, _docs_diff("docs/x.md"))[0] == DEEP


def test_security_kind_still_buys_deep_at_any_severity():
    # AC-3 (#827): unchanged from #807. A security risk somebody rated 'low'
    # is still a security risk, and rating it is not the same as judging it.
    for severity in ("low", "medium", "high"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": "security", "severity": severity}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", severity
        assert "security" in reasons[0]


async def test_process_surface_wins_over_product_risk(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#827): the new rule must not punch a hole in #820. A product risk
    # says nothing about a subprocess left running after its timeout.
    recorder = _DispatchRecorder({"agent": {"id": "bc-pw"}, "run": {"id": "r-pw"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-product-and-process", risks=_PRODUCT_RISK, diff=_DIFF_509
    )

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"


async def test_provider_usage_is_stored_on_the_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#828): the sweep already fetches the bill to compare it. Keeping
    # it is the whole point — the economics were being computed from the
    # harness's own claim while the billed number was thrown away.
    recorder = _DispatchRecorder({"agent": {"id": "bc-bill"}, "run": {"id": "r-bill"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-billed")

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "harness_version": 8,
            "raw_count": 1,
            "findings_confirmed": [
                {"locator": "none", "title": "real one", "severity": "medium"}
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
            "tokens_spent": 175_000,
        },
    )
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        # The real #818 numbers.
        return {"totalUsage": {"totalTokens": 6_013_569}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    await sweep_review_dispatches(db)

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["provider_tokens"] == 6_013_569
    assert saved["tokens_spent"] == 175_000, "the self-report is not overwritten"
    closed = await _any_dispatch_row(db, task_id)
    assert closed["status"] == "done"
    assert closed["provider_tokens"] == 6_013_569, (
        "the bill lives on the dispatch too — a failed run has no report row"
    )


# --- #1856: documents and pinned prod snapshots name a surface, they run none ---

_FIXTURES = Path(__file__).parent / "fixtures" / "process_surface"


def _fixture_diff(name: str) -> str:
    # Full diffs of real commits, saved so the test does not need git in CI.
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _added(path: str, line: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"


def test_docs_and_prod_snapshots_are_not_a_process_surface():
    # AC-1 (#1856): git diff 991d0d94..edba5db8 (#1849) — CAPTURE.txt and README
    # only mention worktree.conf — buys no deep.
    assert process_surface_reasons(_fixture_diff("pr1849_991d0d94_edba5db8.diff")) == []
    # The same words in a document or a snapshot, one line each.
    assert process_surface_reasons(_added("docs/notes.md", "uses worktree")) == []
    assert (
        process_surface_reasons(
            _added("deploy/systemd/prod/etc/x.service.d/worktree.conf", "worktree")
        )
        == []
    )
    # Everything that can run stays a signal.
    for path, line in (
        ("hub/services/x.py", "path = workspace_path / 'worktree'"),
        ("scripts/x.sh", "git worktree add"),
        (".github/workflows/x.yml", "run: git worktree prune"),
        ("deploy/review-runner/run.sh", "git worktree add"),
        ("deploy/systemd/haiplane-x.service", "ExecStart=git worktree add"),
        ("tests/test_x.py", "worktree = 1"),
        # Markdown that is run, not read, and non-snapshot files in the dir.
        ("hub/prompts/run.md", "git worktree add"),
        ("skills/x/SKILL.md", "git worktree add"),
        ("agents/x.md", "git worktree add"),
        ("deploy/systemd/prod/run.sh", "git worktree add"),
    ):
        assert process_surface_reasons(_added(path, line)), path
    # An inert file does not hide a live one beside it.
    mixed = _added("README.md", "worktree") + _added("scripts/x.sh", "git worktree add")
    assert process_surface_reasons(mixed) == ["воркспейс и ветки: worktree"]


def test_historical_process_diffs_keep_their_signal():
    # AC-2 (#1856): the original diffs of #506, #509 and #544 keep subprocess
    # and timeout signals.
    d506 = process_surface_reasons(_fixture_diff("task506_d2c7b7a0.diff"))
    d509 = process_surface_reasons(_fixture_diff("task509_ca8ce75a.diff"))
    d544 = process_surface_reasons(_fixture_diff("task544_4077012a.diff"))
    assert any(r.startswith("запуск подпроцессов") for r in d506)
    assert any(r.startswith("таймауты и отмена") for r in d506)
    assert any(r.startswith("запуск подпроцессов") for r in d509)
    assert any(r.startswith("таймауты и отмена") for r in d509)
    assert any(r.startswith("запуск подпроцессов") for r in d544)


def test_added_plus_plus_line_does_not_hide_the_rest_of_a_file():
    # #1856: an added "++ counters.md" is "+++ counters.md" in the diff. Read as
    # a file header it ended the live .py section and silenced what follows.
    diff = (
        "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
        "@@ -0,0 +1,2 @@\n+++ counters.md\n+    subprocess.run(['ls'])\n"
    )
    assert process_surface_reasons(diff) == ["запуск подпроцессов: subprocess.run"]
    # split_generated reads headers the same way: an added "++ uv.lock" line
    # neither drops the rest of the file nor counts as a dropped file.
    kept, dropped = split_generated(diff)
    assert dropped == [] and kept == diff
    lock = (
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
        "@@ -0,0 +1 @@\n+x\n"
    )
    kept, dropped = split_generated(diff + lock)
    assert dropped == ["uv.lock"] and "subprocess.run" in kept and "+x" not in kept


def test_plain_unified_diff_without_diff_git_keeps_every_header():
    # #1856: no "diff --git" line, so no hunk/header split: each "+++ " opens a
    # file. The lock or the document is dropped, the code after it is read.
    code = (
        "--- a/hub/x.py\n+++ b/hub/x.py\n@@ -0,0 +1 @@\n+    subprocess.run(['ls'])\n"
    )
    lock = "--- a/uv.lock\n+++ b/uv.lock\n@@ -0,0 +1 @@\n+x\n"
    doc = "--- a/docs/a.md\n+++ b/docs/a.md\n@@ -0,0 +1 @@\n+git worktree add\n"
    want = ["запуск подпроцессов: subprocess.run"]
    assert process_surface_reasons(lock + code) == want
    assert process_surface_reasons(doc + code) == want
    kept, dropped = split_generated(lock + code)
    assert dropped == ["uv.lock"] and "subprocess.run" in kept


def test_c_quoted_non_ascii_paths_are_classified_like_ascii_ones():
    # #1856: git prints "b/docs/\320\267.md" for docs/з.md.
    def doc(path: str) -> str:
        return (
            f"diff --git a/x b/x\n--- /dev/null\n+++ {path}\n"
            "@@ -0,0 +1 @@\n+git worktree add\n"
        )

    code = _added("hub/x.py", "subprocess.run(['ls'])")
    want = ["запуск подпроцессов: subprocess.run"]
    assert process_surface_reasons(doc('"b/docs/\\320\\267.md"') + code) == want
    assert process_surface_reasons(doc("b/docs/z.md") + code) == want
    assert process_surface_reasons(doc('"b/hub/\\320\\267.py"')) != []
    kept, dropped = split_generated(doc('"b/node_modules/\\320\\267.js"') + code)
    assert dropped == ["node_modules/з.js"]


_SUBPROCESS = ["запуск подпроцессов: subprocess.run"]
_DOC_HUNK = "--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+text\n"


def test_hunk_body_is_never_a_header():
    # #1856: the edge of a hunk is its counters. An added line whose text is
    # "++ README.md" is "+++ README.md" in the diff and must stay a body line,
    # in a plain unified diff and in a git one.
    body = "@@ -0,0 +1,2 @@\n+++ README.md\n+    subprocess.run(['ls'])\n"
    plain = "--- a/hub/x.py\n+++ b/hub/x.py\n" + body
    git = "diff --git a/hub/x.py b/hub/x.py\n" + plain
    for diff in (plain, git):
        assert process_surface_reasons(diff) == _SUBPROCESS
        assert split_generated(diff) == (diff, [])


def test_combined_diff_after_a_document_opens_a_new_file():
    # #1856: "diff --cc" ends the document before it; "@@@" counts one old
    # side per parent, so the "++" prefixed line is body of hub/x.py.
    combined = (
        "diff --cc hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
        "@@@ -1,1 -1,1 +1,2 @@@\n  x\n++subprocess.run(['ls'])\n"
    )
    for head in ("diff --git a/README.md b/README.md\n", ""):
        assert process_surface_reasons(head + _DOC_HUNK + combined) == _SUBPROCESS
    # Without ---/+++ lines only the "diff --cc" line says a new file began.
    bare = "diff --cc hub/x.py\n@@@ -1,1 -1,1 +1,2 @@@\n  x\n++subprocess.run(['ls'])\n"
    assert process_surface_reasons(_DOC_HUNK + bare) == _SUBPROCESS
    combined2 = combined.replace("diff --cc", "diff --combined")
    assert process_surface_reasons(_DOC_HUNK + combined2) == _SUBPROCESS


def test_hunk_that_does_not_add_up_is_kept_as_code():
    # #1856: counters that run negative, or a hunk cut short by the next file,
    # mean we cannot tell where the file ends: nothing is dropped.
    too_long = (
        "--- a/README.md\n+++ b/README.md\n@@ -0,0 +1 @@\n+a\n+subprocess.run(1)\n"
    )
    assert process_surface_reasons(too_long) == _SUBPROCESS
    short = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -0,0 +1,5 @@\n+subprocess.run(1)\n"
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
        "@@ -0,0 +1 @@\n+x\n"
    )
    assert process_surface_reasons(short) == _SUBPROCESS
    kept, dropped = split_generated(short)
    # Once the counters broke nothing after that is cut: not even a clean lock.
    assert dropped == [] and kept == short
    bad_header = (
        "--- a/README.md\n+++ b/README.md\n@@ nonsense @@\n+subprocess.run(1)\n"
    )
    assert process_surface_reasons(bad_header) == _SUBPROCESS


_COMBINED_BODY = (
    "@@@ -1,2 -1,2 +1,2 @@@\n- old1\n -old2\n--- junk\n+++ README.md\n"
    '++subprocess.run(["ls"])\n'
)


def _combined(path: str, body: str = _COMBINED_BODY) -> str:
    return f"diff --cc {path}\n--- a/{path}\n--- a/{path}\n+++ b/{path}\n{body}"


def test_combined_hunk_counts_each_parent_by_its_own_column():
    # #1856: "-" in a column means the PARENT of that column had the line.
    # Counting every parent on every line ran the counters out early and made
    # "--- junk" / "+++ README.md" look like a new document.
    assert process_surface_reasons(_combined("hub/x.py")) == _SUBPROCESS
    # The same body in a generated file is a normal generated file: cut whole.
    code = _added("hub/y.py", "subprocess.run(1)")
    kept, dropped = split_generated(_combined("uv.lock") + code)
    assert dropped == ["uv.lock"]
    assert "junk" not in kept and "subprocess.run" in kept
    assert process_surface_reasons(_combined("uv.lock") + code) == _SUBPROCESS


def test_diff_is_cut_into_lines_by_lf_only():
    # #1856: git ends a line at LF. A VT, FF or U+2028 inside a literal is text.
    assert _diff_lines("+a\v+b\n+c\x0c\u2028d\r\n") == [
        "+a\v+b\n",
        "+c\x0c\u2028d\r\n",
    ]
    lock = (
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n"
        "@@ -0,0 +1,2 @@\n+x\v+y\n+z\n"
    )
    code = _added("hub/x.py", "subprocess.run(1)")
    kept, dropped = split_generated(lock + code)
    assert dropped == ["uv.lock"] and "x\v" not in kept and code in kept
    assert process_surface_reasons(lock + code) == _SUBPROCESS


def test_after_the_first_mismatch_nothing_is_cut():
    # #1856: not "this file is code" but "the rest of the diff is code". The
    # lock AFTER a broken hunk stays, and so does the document after it.
    broken = "diff --git a/hub/a.py b/hub/a.py\n--- a/hub/a.py\n+++ b/hub/a.py\n@@ -0,0 +1,3 @@\n+a\n"
    tail = (
        _added("uv.lock", "x")
        + _added("docs/a.md", "git worktree add")
        + _added("hub/z.py", "subprocess.run(1)")
    )
    kept, dropped = split_generated(broken + tail)
    assert dropped == [] and kept == broken + tail
    reasons = process_surface_reasons(broken + tail)
    assert reasons == [
        "запуск подпроцессов: subprocess.run",
        "воркспейс и ветки: worktree",
    ]
    # Before the mismatch the same files are cut as usual.
    assert split_generated(tail)[1] == ["uv.lock"]
