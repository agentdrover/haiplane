"""Hub-dispatched cross-model reviews (#757).

The hub calls the reviewer, not the implementer; failures alert once and
change nothing; a run that finished without a report fails loudly; a
report whose tokens disagree with the provider's usage is flagged.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import auth as hub_auth
from hub import config
from hub import repository as repo
from hub import services
from hub.integrations import cursor_cloud
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.project_policy import review_dispatch_enabled
from hub.services.model_family import family
from hub.services.review_dispatch import (
    _REVIEW_MODEL_PREFERENCES,
    REVIEW_FILE_LINE_CAP,
    changed_paths,
    diff_plan,
    file_line_counts,
    is_generated,
    maybe_dispatch_review,
    pick_review_model,
    pick_review_profile,
    rules_candidates,
    rules_char_cap,
    split_generated,
    sweep_review_dispatches,
)

_TIP = "c" * 40


_HARMLESS_DIFF = "+++ b/docs/notes.md\n+одна строка текста\n"


class _PinnedGitOps(NoopGitOps):
    def __init__(
        self,
        tip: str,
        paths: list[str],
        diff: str | None = None,
        rules: dict[str, str] | None = None,
    ) -> None:
        self._tip = tip
        self._paths = paths
        # #820: the profile is decided against the diff, so the double must
        # serve one. None means "could not be read", which buys deep.
        self._diff = _HARMLESS_DIFF if diff is None else diff
        # #873: the repository's review rules, keyed by path. Absent path =
        # no such file, exactly as `git show base:path` behaves.
        self._rules = rules or {}

    async def branch_diff(self, repo, base, branch):
        return self._diff

    async def file_at_ref(self, repo, ref, path):
        return self._rules.get(path)

    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return self._tip

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


class _DispatchRecorder:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _wire(monkeypatch, recorder: _DispatchRecorder) -> None:
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")
    monkeypatch.setattr(cursor_cloud, "create_review_agent", recorder)

    async def _no_usage(agent_id, run_id=None):
        # Default: the API did not answer. Sweep must not hit the network
        # just because _wire set a fake key (#1026 stamps usage on every
        # terminal close). Tests that care override this.
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_usage)


async def _any_dispatch_row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await db.execute_fetchall(
        "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
    )
    assert rows, "no dispatch recorded for the task"
    return dict(rows[-1])


async def _submitted(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    verdict_auto: bool = True,
    policy: dict | None = None,
    repo_name: str = "mrPDA/spike-repo",
    areas: list[str] | None = None,
    risks: list[dict] | None = None,
    clear_risk_class: bool = False,
    diff: str | None = None,
    rules: dict[str, str] | None = None,
) -> int:
    areas = ["docs/notes.md"] if areas is None else areas
    pid = await repo.create_project(
        db,
        slug=slug,
        name=slug.title(),
        repo_name=repo_name,
        workspace_path="/tmp/ws",
    )
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    elif verdict_auto:
        await repo.update_project(db, pid, gate_policy=json.dumps({"verdict": "auto"}))
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(
        db, task_id, TaskRefine(affected_areas=areas, risks=risks)
    )
    if clear_risk_class:
        # A task whose class was never computed: the state #582 calls
        # "not computed", which must never be read as low risk. NULL is that
        # state in the column; the empty string is not a valid class.
        await db.execute("UPDATE tasks SET risk_class = NULL WHERE id = ?", (task_id,))
    await db.commit()

    plugins.git_ops = _PinnedGitOps(_TIP, areas, diff, rules)
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert view.status.value == "review"
    return task_id


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


async def test_usage_mismatch_is_flagged(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#757): the report's tokens are cross-checked against the
    # provider's usage — a big gap is flagged to the audit, the dispatch
    # settles as done either way.
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
        "repo проекта (репозиторий на GitHub); "
        "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера). "
        "Вердикт остаётся человеку (#757)."
    )
    assert not recorder.calls


async def test_each_label_belongs_to_its_own_setting(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1083), the half a conjunction cannot hold: when two settings are
    # missing TOGETHER, swapping their labels leaves both strings in the text
    # and every assertion above stays green — while the operator is sent to
    # fix the setting that was fine. Each label has to be pinned to its own
    # condition, one missing setting at a time.
    recorder = _DispatchRecorder({"agent": {"id": "bc-p"}, "run": {"id": "run-p"}})
    _wire(monkeypatch, recorder)

    only_repo = await _submitted(client, db, "spike-only-repo", repo_name="")
    assert (await _config_alerts(client, only_repo))[0] == (
        "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
        "repo проекта (репозиторий на GitHub). "
        "Вердикт остаётся человеку (#757)."
    )

    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "")
    only_token = await _submitted(client, db, "spike-only-token")
    assert (await _config_alerts(client, only_token))[0] == (
        "Кросс-модельное ревью НЕ вызвано: не хватает конфигурации — "
        "CURSOR_REVIEWER_HUB_TOKEN (токен ревьюера). "
        "Вердикт остаётся человеку (#757)."
    )
    assert not recorder.calls


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


# --- Review profiles (#807) --------------------------------------------------
#
# The profile answers "how much was this run allowed to spend", and it is
# decided by the hub before the run starts. Every kind of ignorance —
# unknown class, unreadable class, a human explicitly asking — resolves
# toward deep: cheap is the default only where the facts say it is safe.


async def _dispatch_row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await repo.list_active_review_dispatches(db)
    mine = [dict(r) for r in rows if r["task_id"] == task_id]
    assert mine, "no dispatch recorded for the task"
    return mine[-1]


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
    # AC-2 (#807): a migration-class change and a declared high risk each
    # buy the expensive harness on their own.
    recorder = _DispatchRecorder({"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}})
    _wire(monkeypatch, recorder)

    by_class = await _submitted(client, db, "spike-deep-class", areas=["hub/db.py"])
    assert (await _dispatch_row(db, by_class))["profile"] == "deep"
    assert "multi-agent-review" in recorder.calls[0]["prompt_text"]

    by_risk = await _submitted(
        client,
        db,
        "spike-deep-risk",
        # #827: a TECHNICAL high risk. A product one no longer buys the
        # harness — see test_product_high_risk_does_not_buy_deep.
        risks=[{"kind": "breaking_change", "severity": "high", "description": "d"}],
    )
    assert (await _dispatch_row(db, by_risk))["profile"] == "deep", (
        "a declared technical high risk is what the expensive harness is for"
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
        client, db, "spike-unclassified", areas=[], clear_risk_class=True
    )
    row = dict(await repo.get_task(db, task_id))
    assert not row["risk_class"], "the fixture must leave the class uncomputed"

    assert (await _dispatch_row(db, task_id))["profile"] == "deep"
    # And the same for a class the enum cannot read at all.
    # #820: the rule now answers with its reasons, and judges against a diff.
    assert pick_review_profile({"risk_class": "R99"}, _HARMLESS_DIFF)[0] == "deep"
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


# --- Process surfaces buy the expensive profile (#820) -----------------------
#
# Measured, not assumed: the lite-vs-deep comparison of 21.08.2026 found the
# cheap profile caught 2 of 7 confirmed findings, and both misses were process
# defects — an orphaned collector and a collection run against the wrong
# branch. Neither is visible in the diff text; both live on surfaces that can
# be named.

# Real added lines from the branches where those defects were found. Written
# out rather than generated, so the regression is against what actually
# happened rather than against a shape invented to pass.
_DIFF_506 = (
    "+++ b/hub/services/test_existence.py\n"
    "+        proc = await asyncio.create_subprocess_exec(\n"
    '+            "uv",\n'
    "+            stdout=asyncio.subprocess.PIPE,\n"
    "+        )\n"
    "+        out, _ = await asyncio.wait_for("
    "proc.communicate(), timeout=_COLLECT_TIMEOUT)\n"
)
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


async def test_product_high_risk_does_not_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#827): the real #818 risk, verbatim. It stays on the class.
    recorder = _DispatchRecorder({"agent": {"id": "bc-pr"}, "run": {"id": "r-pr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-product-risk", risks=_PRODUCT_RISK)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite"


def test_technical_high_risk_buys_deep_with_named_kind():
    # AC-2 (#827): technical high still buys the harness, and the reason says
    # WHICH kind — "high" alone was never enough to argue with.
    for kind in ("breaking_change", "data_migration", "performance"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": kind, "severity": "high"}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", kind
        assert kind in reasons[0], f"the reason must name the kind: {reasons}"


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


def test_unknown_risk_kind_at_high_stays_deep():
    # AC-5 (#827): not knowing what a risk is must never be the cheap answer
    # (#582) — and it closes the obvious way around the rule.
    for kind in ("", "какой-то-новый-вид"):
        profile, reasons = pick_review_profile(
            {
                "risk_class": "R0",
                "risks": json.dumps([{"kind": kind, "severity": "high"}]),
            },
            _HARMLESS_DIFF,
        )
        assert profile == "deep", repr(kind)
        assert "нераспознанным" in reasons[0]


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


# --- Repository review rules in the prompt (#873) ---------------------------
# The reviewer used to read the diff knowing nothing about the code it came
# from, while _PROCESS_SURFACES already listed the classes this repository
# burns itself on and spent them on routing alone.

_RULES_DIFF = "+++ b/hub/services/notes.py\n+одна строка кода\n"


def test_changed_paths_ignores_deletions_and_dedupes():
    diff = (
        "+++ b/hub/a.py\n+x\n+++ /dev/null\n+++ b/hub/a.py\n+y\n+++ b/docs/b.md\n+z\n"
    )
    assert changed_paths(diff) == ["hub/a.py", "docs/b.md"]


def test_rules_candidates_root_first_nearest_last():
    # The root file applies even to a diff nobody could read, and the file
    # closest to the change is read last so it can override.
    assert rules_candidates([]) == [".hub/REVIEW_RULES.md"]
    assert rules_candidates(["hub/services/notes.py"]) == [
        ".hub/REVIEW_RULES.md",
        "hub/.hub/REVIEW_RULES.md",
        "hub/services/.hub/REVIEW_RULES.md",
    ]


async def test_repo_rules_collected_up_the_tree(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#873): a root rules file and one in the changed file's directory
    # both reach the prompt, with the nearest one last.
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-rules"}, "run": {"id": "r-rules"}}
    )
    _wire(monkeypatch, recorder)

    await _submitted(
        client,
        db,
        "spike-rules",
        diff=_RULES_DIFF,
        rules={
            ".hub/REVIEW_RULES.md": "ПРАВИЛО-КОРНЕВОЕ",
            "hub/services/.hub/REVIEW_RULES.md": "ПРАВИЛО-БЛИЖНЕЕ",
        },
    )

    prompt = recorder.calls[0]["prompt_text"]
    assert "ПРАВИЛО-КОРНЕВОЕ" in prompt and "ПРАВИЛО-БЛИЖНЕЕ" in prompt
    assert prompt.index("ПРАВИЛО-КОРНЕВОЕ") < prompt.index("ПРАВИЛО-БЛИЖНЕЕ"), (
        "the rules nearest the changed file must be read last"
    )
    # The framing is part of the contract, not decoration: a list presented as
    # exhaustive becomes the ceiling of the reviewer's attention.
    assert "СМОТРИ ШИРЕ" in prompt


async def test_missing_repo_rules_is_stated_not_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#873): no rules file is a NAMED absence — in the prompt and in the
    # task update. Silence there reads as "nothing ever broke here".
    recorder = _DispatchRecorder({"agent": {"id": "bc-norules"}, "run": {"id": "r-nr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-norules", diff=_RULES_DIFF)

    prompt = recorder.calls[0]["prompt_text"]
    assert "ПРАВИЛ РЕПОЗИТОРИЯ НЕТ" in prompt
    assert "отсутствие данных" in prompt
    updates = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    dispatched = [u for u in updates if "Кросс-модельное ревью вызвано" in u["content"]]
    assert dispatched and "правил нет" in dispatched[-1]["content"]


async def test_repo_rules_are_capped(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#873): rules cannot eat the budget they were written to protect.
    # What did not fit is named — an unread rule is "not checked", not "absent".
    recorder = _DispatchRecorder({"agent": {"id": "bc-cap"}, "run": {"id": "r-cap"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_LITE_TOKEN_BUDGET", 400)
    assert rules_char_cap() == 400, "a quarter of the ceiling, counted in chars"

    task_id = await _submitted(
        client,
        db,
        "spike-cap",
        diff=_RULES_DIFF,
        rules={
            ".hub/REVIEW_RULES.md": "КОРОТКОЕ-ПРАВИЛО",
            "hub/services/.hub/REVIEW_RULES.md": "Д" * 500,
        },
    )

    prompt = recorder.calls[0]["prompt_text"]
    assert "КОРОТКОЕ-ПРАВИЛО" in prompt, "what fits is still delivered"
    assert "Д" * 500 not in prompt
    assert "ОБРЕЗАНО" in prompt
    assert "hub/services/.hub/REVIEW_RULES.md" in prompt, "the dropped file is named"
    updates = [dict(r) for r in await repo.get_task_updates(db, task_id)]
    dispatched = [u for u in updates if "Кросс-модельное ревью вызвано" in u["content"]]
    assert dispatched and "обрезано" in dispatched[-1]["content"]


# --- Diff hygiene: the budget goes on code (#874) ---------------------------
#
# The hub does not hand the reviewer a diff — it reads one for itself and tells
# the reviewer to run git. So for the reviewer the exclusion list is a pathspec
# in the command it is given plus the names of what was left out; inside the
# hub it is a real filter over the diff that picks the profile.

_LOCK_DIFF = (
    "diff --git a/uv.lock b/uv.lock\n"
    "--- a/uv.lock\n"
    "+++ b/uv.lock\n"
    "+    asyncio.gather(everything)\n"
    "diff --git a/hub/services/notes.py b/hub/services/notes.py\n"
    "--- a/hub/services/notes.py\n"
    "+++ b/hub/services/notes.py\n"
    "+одна строка кода\n"
)


def test_is_generated_knows_artefacts_from_code():
    assert is_generated("uv.lock") and is_generated("web/package-lock.json")
    assert is_generated("tests/__snapshots__/card.txt")
    assert is_generated("static/app.min.js")
    # The list must stay narrow: a broad mask would hide real code silently.
    assert not is_generated("hub/services/lockfile_reader.py")
    assert not is_generated("hub/config.json")


def test_split_generated_drops_only_the_artefact_hunks():
    kept, dropped = split_generated(_LOCK_DIFF)
    assert dropped == ["uv.lock"]
    assert "asyncio.gather(everything)" not in kept
    assert "одна строка кода" in kept


def test_file_line_counts_orders_by_size():
    diff = "+++ b/a.py\n+1\n+2\n+3\n+++ b/b.py\n-1\n+++ /dev/null\n-x\n"
    assert file_line_counts(diff) == [("a.py", 3), ("b.py", 1)]


def test_generated_files_excluded_and_named():
    # AC-1 (#874): the reviewer gets a command it can run as given, and every
    # exclusion is named where it will read it. A quiet exclusion would read as
    # "there was nothing there" (#824).
    block, note = diff_plan(_LOCK_DIFF, "develop", "task-874/x")

    assert "git diff develop...task-874/x --" in block
    assert "':(exclude)uv.lock'" in block
    assert "uv.lock" in block and "сгенерированны" in block
    assert "исключено сгенерированных: 1" in note


def test_unreadable_diff_says_so_instead_of_excluding_nothing():
    # "We could not read it" and "there was nothing to exclude" are different
    # answers, and the second one reads as a clean, complete subject (#725).
    block, note = diff_plan(None, "develop", "task-874/x")
    assert "не удалось" in block
    assert ":(exclude)" not in block
    assert "не прочитан" in note


async def test_generated_file_marker_does_not_buy_deep(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#874): a marker inside a lock file is not code anyone wrote. Before
    # this, one such line was the cheapest possible way to buy the expensive
    # harness — and nobody would have noticed the bill.
    recorder = _DispatchRecorder({"agent": {"id": "bc-gen"}, "run": {"id": "r-gen"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-generated", diff=_LOCK_DIFF)

    assert (await _dispatch_row(db, task_id))["profile"] == "lite", (
        "asyncio.gather in uv.lock must not buy deep"
    )
    # And the same marker in real code still does.
    real = _LOCK_DIFF.replace("+++ b/uv.lock", "+++ b/hub/services/pool.py")
    other = await _submitted(client, db, "spike-real-marker", diff=real)
    assert (await _dispatch_row(db, other))["profile"] == "deep"


async def test_oversized_file_is_named_not_left_to_eat_budget(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#874): one huge file must not silently consume the pass the other
    # files were waiting for. The hub has no diff to truncate, so it names the
    # file and its size and demands the remainder in lost_dimensions.
    recorder = _DispatchRecorder({"agent": {"id": "bc-big"}, "run": {"id": "r-big"}})
    _wire(monkeypatch, recorder)
    big = "+++ b/hub/services/huge.py\n" + "+строка\n" * (REVIEW_FILE_LINE_CAP + 5)
    small = "+++ b/hub/services/small.py\n+одна строка\n"

    await _submitted(client, db, "spike-oversized", diff=big + small)

    prompt = recorder.calls[0]["prompt_text"]
    assert "НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД" in prompt
    assert f"hub/services/huge.py ({REVIEW_FILE_LINE_CAP + 5} строк)" in prompt
    assert "lost_dimensions" in prompt
    assert "hub/services/small.py" not in prompt.split("НЕ ПОМЕСТЯТСЯ")[1], (
        "the small file is not the one that did not fit"
    )
    assert "git diff" in prompt, "the rest of the subject is still under review"


# --- The ladder: buy deep when cheap said it did not finish (#879) ----------
#
# The trigger is the reviewer's own declaration of what it did not read. #893
# removed the budget guard because a run that burned 1.5M while reporting 36k
# sailed through as complete; the declaration, unlike the number, is checkable
# against the diff.


async def _machine_report(
    client: AsyncClient,
    task_id: int,
    *,
    incomplete: bool,
    confirmed: list | None = None,
) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 12000,
            "model": "grok-4.6",
            "raw_count": 1,
            "findings_confirmed": confirmed or [],
            "findings_rejected": [],
            "incomplete": incomplete,
            "unresolved": [],
            "lost_dimensions": ["hub/services/big.py"] if incomplete else [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text


async def test_incomplete_lite_report_buys_a_deep_top_up(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#879): a cheap run that says it did not finish is topped up in the
    # SAME generation, and the human is not handed the unfinished work.
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup")
    assert (await _dispatch_row(db, task_id))["profile"] == "lite"

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "the incomplete run bought a second one"
    row = await _dispatch_row(db, task_id)
    assert row["profile"] == "deep"
    task = dict(await repo.get_task(db, task_id))
    assert row["submission_generation"] == task["submission_generation"], (
        "the top-up belongs to the submission it tops up"
    )
    assert task["status"] == "review", "still under review, not handed over"
    assert "multi-agent-review" in recorder.calls[1]["prompt_text"]


async def test_complete_lite_report_buys_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The ladder must not fire on a run that finished. Otherwise "lite by
    # default" becomes "always both", which costs more than always-deep did.
    recorder = _DispatchRecorder({"agent": {"id": "bc-2"}, "run": {"id": "r-2"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-complete")

    await _machine_report(client, task_id, incomplete=False)

    assert len(recorder.calls) == 1, "a finished run buys no top-up"


async def test_escalation_ladder_has_a_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#879): a second incomplete report does NOT buy a third run. The
    # human gets a named cause instead of silence — a reviewer that keeps
    # declaring itself unfinished is a problem to look at, not to fund.
    recorder = _DispatchRecorder({"agent": {"id": "bc-3"}, "run": {"id": "r-3"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-ceiling")

    await _machine_report(client, task_id, incomplete=True)
    assert len(recorder.calls) == 2
    # The top-up itself comes back unfinished.
    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 2, "the ceiling holds at two runs"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [u for u in updates if u["kind"] == "alert" and "потолок" in u["content"]]
    assert alerts, "the ceiling is announced, not silent"
    assert "решение за человеком" in alerts[-1]["content"].lower()


async def test_top_up_never_fires_on_an_unknown_profile(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "We do not know how this was reviewed" is not "it was reviewed cheaply"
    # (#807). Topping up an unknown profile would buy a run on a guess.
    recorder = _DispatchRecorder({"agent": {"id": "bc-4"}, "run": {"id": "r-4"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-unknown-profile")
    await db.execute(
        "UPDATE review_dispatches SET profile = '' WHERE task_id = ?", (task_id,)
    )
    await db.commit()

    await _machine_report(client, task_id, incomplete=True)

    assert len(recorder.calls) == 1, "an unknown profile buys nothing"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alerts = [
        u
        for u in updates
        if u["kind"] == "alert" and "добор не положен" in u["content"]
    ]
    assert alerts and "не заявлен" in alerts[-1]["content"], (
        "the refusal names its cause instead of passing in silence"
    )


# --- Incremental review: pay for the fixes, not the branch (#880) ------------
#
# A report is pinned to a generation, so a resubmission makes it stale — right
# in substance, expensive in practice: the next run re-read the whole branch,
# including code the previous generation had already read and called clean.


class _AncestryGitOps(_PinnedGitOps):
    """Git that answers the three questions the delta stands on."""

    def __init__(self, *args, ancestor: bool | None = True, delta: str = "", **kw):
        super().__init__(*args, **kw)
        self._ancestor = ancestor
        self._delta = delta

    async def is_ancestor(self, repo, ancestor, descendant):
        return self._ancestor

    async def branch_diff(self, repo, base, branch):
        # The delta call passes the previous SHA as `base`; anything else is
        # the ordinary branch diff.
        if base == _PREV_SHA:
            return self._delta
        return self._diff


_PREV_SHA = "b" * 40
_DELTA = "+++ b/hub/services/fixed.py\n+исправление\n"


async def _second_generation(
    client: AsyncClient,
    db: aiosqlite.Connection,
    slug: str,
    *,
    ancestor: bool | None = True,
    base_branch: str = "develop",
    record_previous: bool = True,
) -> int:
    """A task on its SECOND submission, with the first one in the ledger."""
    task_id = await _submitted(client, db, slug)
    if record_previous:
        # submit_for_review already wrote this row; the upsert pins the sha and
        # base the test wants to reason about.
        await repo.record_submission(
            db,
            task_id=task_id,
            generation=1,
            sha=_PREV_SHA,
            base_branch=base_branch,
        )
    else:
        # A task already in flight when the ledger appeared has no row at all.
        await db.execute("DELETE FROM submissions WHERE task_id = ?", (task_id,))
    # Bump to generation 2 the way a resubmission would, and pin a new tip.
    await db.execute(
        "UPDATE tasks SET submission_generation = 2, submission_sha = ? WHERE id = ?",
        ("c" * 40, task_id),
    )
    await db.commit()
    return task_id


async def test_submit_records_the_generation_in_the_ledger(
    client: AsyncClient, db: aiosqlite.Connection
):
    # The delta rests entirely on this row, and it is written by the real
    # submission path — not by the tests that reason about it. Asserted here
    # on purpose: every fallback in generation_delta is SAFE, so if the write
    # disappeared nothing would go red. Reviews would quietly go back to
    # reading the whole branch every round, and the only trace would be a
    # reason line saying the previous submission was never recorded.
    task_id = await _submitted(client, db, "spike-ledger")

    row = await repo.previous_submission(db, task_id, generation=2)

    assert row is not None, "submit_for_review must write the submission it pinned"
    recorded = dict(row)
    assert recorded["generation"] == 1
    assert recorded["sha"] == _TIP, "the ledger pins the tip the submission pinned"
    assert recorded["base_branch"], "the base travels with the sha or the delta lies"


async def test_resubmission_reviews_generation_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#880): the second run reads the files this round of fixes touched,
    # whole and against the BASE — and says that is what it read.
    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-delta")
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "'hub/services/fixed.py'" in prompt, "the command is narrowed to the delta"
    assert "прочитана ДЕЛЬТА" in prompt
    assert "дельта к поколению #1" in prompt
    # Whole files against the base: old code beside the new edit stays visible.
    assert "git diff develop...task-" in prompt
    assert "ЦЕЛИКОМ и против базовой ветки" in prompt


async def test_rebase_invalidates_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#880): after a rebase the two commits no longer share a history,
    # so "what changed since last time" is unanswerable — full diff, and the
    # reason is named rather than left to be inferred from a wider command.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-rebase")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], ancestor=False, delta=_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "не предок текущего" in prompt and "перебазировали" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt


async def test_changed_base_branch_invalidates_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The same two commits answer a different question once the base moves.
    recorder = _DispatchRecorder({"agent": {"id": "bc-b"}, "run": {"id": "r-b"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-base", base_branch="main")
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "базовая ветка сменилась" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt


async def test_unrecorded_previous_submission_reads_everything(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # Tasks that were already in flight when the ledger appeared have no
    # previous row. Claiming a delta against a commit nobody recorded would be
    # reading less than the report says.
    recorder = _DispatchRecorder({"agent": {"id": "bc-n"}, "run": {"id": "r-n"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(
        client, db, "spike-noledger", record_previous=False
    )
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "предыдущая сдача не записана" in prompt
    assert "прочитан ВЕСЬ дифф" in prompt


async def test_unreadable_ancestry_reads_everything(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # "Could not look" is not "they are related" (#725). An unanswerable
    # ancestry question must widen the review, never narrow it.
    recorder = _DispatchRecorder({"agent": {"id": "bc-u"}, "run": {"id": "r-u"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-unknown-ancestry")
    plugins.git_ops = _AncestryGitOps(
        _TIP, ["docs/notes.md"], ancestor=None, delta=_DELTA
    )

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "историю проверить не удалось" in prompt
    assert "прочитана ДЕЛЬТА" not in prompt


async def test_previous_findings_travel_with_the_delta(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # The delta tells the reviewer to skip files it did not touch — so what the
    # last run confirmed has to arrive with it, or the fixes go unchecked.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f"}, "run": {"id": "r-f"}})
    _wire(monkeypatch, recorder)
    task_id = await _second_generation(client, db, "spike-prior")
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="lite-diff-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": "race on retry", "severity": "high", "file": "hub/a.py"}]
        ),
        incomplete=False,
    )
    await db.commit()
    plugins.git_ops = _AncestryGitOps(_TIP, ["docs/notes.md"], delta=_DELTA)

    assert await maybe_dispatch_review(db, task_id)

    prompt = recorder.calls[-1]["prompt_text"]
    assert "НА ПРОШЛОМ ПОКОЛЕНИИ БЫЛИ ПОДТВЕРЖДЕНЫ" in prompt
    assert "hub/a.py: race on retry" in prompt
    assert "не считай их закрытыми по факту" in prompt


def test_instance_base_url_never_names_the_vendor_host(monkeypatch):
    """#1005: this hub answers with its own address, never with the authors'.

    The fallback that used to sit here could not fire — ``hub_base_url``
    always answers, if only with ``http://HOST:PORT`` — so a constant naming
    the vendor's server was dead code that would have become a live default
    the moment the branch above it changed. The reviewer agent is sent to the
    URL this function returns, and sending somebody else's reviewer to our hub
    is the failure this test exists to prevent.
    """
    from hub.services.review_dispatch import instance_base_url

    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://hub.example.test")
    assert instance_base_url() == "https://hub.example.test"

    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    resolved = instance_base_url()
    assert resolved, "an unconfigured hub still answers with its own bind address"

    # The half that actually discriminates. Everything above stays green on the
    # pre-fix line as well: ``hub_base_url`` never returns an empty string —
    # HUB_HOST/HUB_PORT are resolved at import and always compose one — so the
    # ``or "<vendor host>"`` branch could not fire and both implementations
    # answered identically. Only an echo that carries NO base_url at all
    # reaches the branch the fix removed, and there the old code named the
    # authors' server. Mutating this module back to the pre-fix line must turn
    # this assertion red; if it does not, the test has stopped guarding #1005.
    import hub.hub_instance as hub_instance

    monkeypatch.setattr(
        hub_instance,
        "instance_echo_fields",
        lambda: {"instance": "local", "server_id": "somebody-elses-box"},
    )
    assert instance_base_url() == ""


# --- Report ↔ dispatch identity (#1025) --------------------------------------
#
# The sweep used to accept ANY report of the right task+generation as the
# dispatched run's own: the author's parallel report closed the hub's dispatch
# as done, silenced the no-report alert and fed the author's numbers to the
# usage cross-check (#1011 gen 1). The dispatch now pins the reviewer
# principal from the TOKEN and matches reports on it — rung by rung within
# the ladder; a dispatch without a pin (history, unresolved token, open mode)
# keeps the old rule.
#
# Auth is real in these tests (open mode off): the pin resolves only where
# intake can read the bearer — in open mode every report lands with
# principal_id NULL, so pinning there would make the dispatch unsatisfiable.


_FOREIGN_REPORT = {
    "harness_skill": "lite-diff-review",
    "raw_count": 1,
    "findings_confirmed": [],
    "findings_rejected": [{"title": "x", "category": "correctness", "reason": "no"}],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent": "author-run-harness",
    "model": "claude-fable-5",
    "tokens_spent": 1000,
}


async def _agent_key(db, username: str) -> tuple[int, str]:
    """A db-backed agent principal and its plaintext API key."""
    from hub.services import admin as admin_svc

    principal = await admin_svc.create_principal(db, kind="agent", username=username)
    key = await admin_svc.create_api_key(db, principal["id"], name=username)
    return principal["id"], key["plaintext_key"]


async def _pinned_setup(db, monkeypatch) -> tuple[int, str, str]:
    """Real auth + a reviewer principal the dispatcher will pin.

    Returns (reviewer_principal_id, reviewer_token, author_token). Call after
    _wire and before _submitted — the pin resolves at dispatch creation.
    """
    from hub import auth as hub_auth

    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    reviewer_pid, reviewer_token = await _agent_key(db, "cloud-reviewer")
    _, author_token = await _agent_key(db, "parallel-author")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", reviewer_token)
    return reviewer_pid, reviewer_token, author_token


async def test_foreign_report_does_not_settle_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1025): a foreign report of the right generation leaves the
    # dispatch waiting, skips the usage cross-check and is named in the feed
    # exactly once.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f1"}, "run": {"id": "run-f1"}})
    _wire(monkeypatch, recorder)
    expected_pid, _, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(
        client, db, "spike-foreign", policy={"review": "dispatch"}
    )
    assert (await _dispatch_row(db, task_id))["reviewer_principal_id"] == expected_pid

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_FOREIGN_REPORT,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text

    usage_calls: list[str] = []

    async def _usage(agent_id, run_id=None):
        usage_calls.append(agent_id)
        return {"totalUsage": {"totalTokens": 100_000}}

    async def _running(agent_id, run_id):
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    monkeypatch.setattr(cursor_cloud, "get_run", _running)

    await sweep_review_dispatches(db)
    assert await repo.list_active_review_dispatches(db), "dispatch keeps waiting"
    assert not usage_calls, "a foreign report must not feed the cross-check"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "другого принципала" in u["content"]]) == 1, (
        "the collision is named once, at intake"
    )
    assert not [u for u in updates if "расходится с данными провайдера" in u["content"]]


async def test_own_report_settles_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1025): the pinned principal's report settles the dispatch, goes
    # through the usage cross-check, and the provider's bill lands on THAT
    # report's row — with no collision noise in the feed.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f2"}, "run": {"id": "run-f2"}})
    _wire(monkeypatch, recorder)
    expected_pid, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-own", policy={"review": "dispatch"})

    own = dict(_FOREIGN_REPORT, agent="cloud-reviewer", model="grok-4.6")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["principal_id"] == expected_pid, "owner recorded from the token"

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    assert not await repo.list_active_review_dispatches(db), "dispatch settles"
    stamped = dict(await repo.get_latest_machine_review(db, task_id))
    assert stamped["provider_tokens"] == 100_000, "the bill lands on the matched row"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "расходится с данными провайдера" in u["content"]]
    assert not [u for u in updates if "другого принципала" in u["content"]]
    assert not [u for u in updates if "Второй отчёт" in u["content"]], (
        "the dispatched run's own report must pass without ceremony"
    )


async def test_grace_alert_fires_despite_foreign_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1025): the exact #1011 shape — terminal run, only a foreign
    # report, grace expired. The safety net must fire instead of being
    # silenced by the foreign report.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f3"}, "run": {"id": "run-f3"}})
    _wire(monkeypatch, recorder)
    _, _, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-grace", policy={"review": "dispatch"})

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=_FOREIGN_REPORT,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)

    await sweep_review_dispatches(db)
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert len([u for u in updates if "отчёт НЕ сдан" in u["content"]]) == 1
    assert not await repo.list_active_review_dispatches(db)


async def test_top_up_ignores_foreign_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1025): a foreign incomplete report buys no top-up; the pinned
    # run's own incomplete report still does — and the deep rung then waits
    # for ITS OWN report instead of settling on the lite one.
    recorder = _DispatchRecorder({"agent": {"id": "bc-f4"}, "run": {"id": "run-f4"}})
    _wire(monkeypatch, recorder)
    _, reviewer_token, author_token = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-topup", policy={"review": "dispatch"})
    assert len(recorder.calls) == 1

    foreign = dict(_FOREIGN_REPORT, incomplete=True)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=foreign,
        headers={"Authorization": f"Bearer {author_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(recorder.calls) == 1, "no ladder step on a foreign declaration"

    own = dict(
        _FOREIGN_REPORT, incomplete=True, agent="cloud-reviewer", model="grok-4.6"
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(recorder.calls) == 2, "our own incomplete run buys the deep top-up"

    # Both rungs pin the same principal. The lite report must settle only
    # the lite rung — before rung-ordered matching the SAME report settled
    # the deep dispatch too, before its run ever reported (#1025 review).
    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    async def _running(agent_id, run_id):
        return {"id": run_id, "status": "RUNNING"}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)
    monkeypatch.setattr(cursor_cloud, "get_run", _running)

    await sweep_review_dispatches(db)
    active = [dict(r) for r in await repo.list_active_review_dispatches(db)]
    assert len(active) == 1, "the deep rung keeps waiting for its own report"
    settled = await repo.get_settled_review_dispatch(db, task_id, 1)
    assert settled is not None and int(dict(settled)["id"]) != int(active[0]["id"])


async def test_open_mode_pins_nothing(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1029): open mode never reads the bearer, so every report lands
    # with principal_id NULL. Pinning a dispatch there would make it
    # unsatisfiable — its own reviewer's report would be flagged foreign and
    # the dispatch would die by a FALSE "отчёт НЕ сдан". So no pin is taken,
    # and the old task+generation rule stands: degradation must never be
    # worse than the behaviour it degrades from.
    recorder = _DispatchRecorder({"agent": {"id": "bc-om"}, "run": {"id": "run-om"}})
    _wire(monkeypatch, recorder)
    # A resolvable reviewer key EXISTS in the db — the only thing missing is
    # an auth mode that would let a report carry its owner.
    _, reviewer_token = await _agent_key(db, "cloud-reviewer")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", reviewer_token)
    assert hub_auth._is_open_mode(), "the fixture client runs in open mode"

    task_id = await _submitted(
        client, db, "spike-openmode", policy={"review": "dispatch"}
    )
    dispatch = await _dispatch_row(db, task_id)
    assert dispatch["reviewer_principal_id"] is None, "no pin under open mode"

    # And the legacy rule still settles the dispatch: any report of the
    # generation counts, exactly as before #1025.
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review", json=_FOREIGN_REPORT
    )
    assert resp.status_code == 200, resp.text

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 100_000}}

    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)

    await sweep_review_dispatches(db)
    assert not await repo.list_active_review_dispatches(db), "legacy rule settles"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "другого принципала" in u["content"]], (
        "an unpinned dispatch must not accuse anyone of being foreign"
    )


# --- Report delivered in the run's own text (#1036) --------------------------
#
# Since 22.08 Cursor stops delivering the hub's MCP into cloud runs: verified
# on grok-4.6, grok-4.5, composer-2.5 and default, and through GetDynamicTools,
# whose catalog does not list the server at all. The reviewers still work — the
# findings just die in the final text. So the text becomes a second, weaker
# delivery path, and its weakness is recorded rather than smoothed over.


def _report_block(**over) -> str:
    payload = {
        "raw_count": 2,
        "incomplete": False,
        "findings_confirmed": [
            {
                "title": "race on retry",
                "severity": "medium",
                "category": "correctness",
                "locator": "lines",
                "file": "hub/a.py",
                "start_line": 10,
            }
        ],
        "findings_rejected": [],
        "unresolved": [],
        "lost_dimensions": [],
        "harness_skill": "lite-diff-review",
        "tokens_spent": 40000,
        "model": "grok-4.6",
        "orchestrator": "cursor-cloud",
    }
    payload.update(over)
    return (
        "Прочитал дифф, ревью выполнено.\n\n"
        "```haiplane-review\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    )


async def _finished_run_with(monkeypatch, text: str) -> None:
    async def _finished(agent_id, run_id):
        return {"id": run_id, "status": "FINISHED", "result": text}

    async def _usage(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 900_000}}

    monkeypatch.setattr(cursor_cloud, "get_run", _finished)
    monkeypatch.setattr(cursor_cloud, "get_usage", _usage)


async def _expire_grace(db) -> None:
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()


async def test_report_recovered_from_run_result(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1036): a finished run with no contract report, but a valid block
    # in its text — the report is stored, marked as transcribed, and the
    # dispatch settles instead of failing.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r1"}, "run": {"id": "run-r1"}})
    _wire(monkeypatch, recorder)
    reviewer_pid, _, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-rec", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())

    await sweep_review_dispatches(db)

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["raw_count"] == 2
    assert saved["principal_id"] == reviewer_pid, "owner is the dispatch's reviewer"
    assert saved["orchestrator"].startswith("cursor-cloud-result"), (
        "origin must live in the data, not only in a feed line"
    )
    assert not await repo.list_active_review_dispatches(db), "dispatch settles"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "восстановлен из текста" in u["content"]]
    assert not [u for u in updates if "отчёт НЕ сдан" in u["content"]]


async def test_unparsable_result_is_kept_as_text_not_invented(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#1036): prose without a block stores NOTHING. The text survives in
    # the feed and the dispatch fails exactly as before — "could not read" must
    # never become "read and clean".
    recorder = _DispatchRecorder({"agent": {"id": "bc-r2"}, "run": {"id": "run-r2"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-prose", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(
        monkeypatch,
        "Нашёл две проблемы: гонка в ретраях и незакрытый файл. Отчёт сдать "
        "не смог, MCP не примонтирован.",
    )

    await sweep_review_dispatches(db)

    assert await repo.get_latest_machine_review(db, task_id) is None, (
        "findings must never be invented out of prose"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert [u for u in updates if "нет разбираемого блока" in u["content"]]
    assert [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert not await repo.list_active_review_dispatches(db), (
        "the dispatch still fails — nothing was recovered"
    )


async def test_mcp_report_wins_over_result_fallback(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1036): when the contract path worked, the text is not read at all
    # — one submission must not end up with two reports.
    recorder = _DispatchRecorder({"agent": {"id": "bc-r3"}, "run": {"id": "run-r3"}})
    _wire(monkeypatch, recorder)
    _, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-both", policy={"review": "dispatch"})

    own = dict(_FOREIGN_REPORT, agent="cloud-reviewer", model="grok-4.6")
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json=own,
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 200, resp.text
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block(raw_count=99))

    await sweep_review_dispatches(db)

    rows = await repo.machine_reviews_of_generation(db, task_id, 1)
    assert len(rows) == 1, "the contract report stands alone"
    assert dict(rows[0])["raw_count"] != 99


async def test_recovered_report_settles_its_own_dispatch(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#1036): the transcribed report must read as the dispatch's OWN on
    # the next pass too — a foreign-looking one would raise the false "report
    # not submitted" alert this area already fixed once (#1025).
    recorder = _DispatchRecorder({"agent": {"id": "bc-r4"}, "run": {"id": "run-r4"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    task_id = await _submitted(client, db, "spike-own2", policy={"review": "dispatch"})
    await _expire_grace(db)
    await _finished_run_with(monkeypatch, _report_block())

    await sweep_review_dispatches(db)
    await sweep_review_dispatches(db)  # second pass must find nothing to do

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert not [u for u in updates if "отчёт НЕ сдан" in u["content"]]
    assert not [u for u in updates if "другого принципала" in u["content"]]
    assert len([u for u in updates if "восстановлен из текста" in u["content"]]) == 1


def test_pick_review_model_stays_within_available_models(monkeypatch):
    # AC-5 (#1036): every preference must be a model the account can actually
    # launch. Before this, a grok implementer sent the diversity rule to
    # gpt-5.3-codex, which refuses at creation with usage_limit_exceeded.
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    available = {"grok-4.6", "grok-4.5", "composer-2.5"}

    assert set(_REVIEW_MODEL_PREFERENCES) <= available
    assert pick_review_model("claude-opus-5") in available
    grok_reviewer = pick_review_model("grok-4.6")
    assert grok_reviewer in available
    assert family(grok_reviewer) != family("grok-4.6"), "diversity still holds"
