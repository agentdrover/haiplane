"""Hub-dispatched cross-model reviews (#757).

The hub calls the reviewer, not the implementer; failures alert once and
change nothing; a run that finished without a report fails loudly; a
report whose tokens disagree with the provider's usage is flagged.
"""

from __future__ import annotations

import asyncio
import json
import re

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import auth as hub_auth
from hub import config
from hub import repository as repo
from hub import services
from hub.integrations import cursor_cloud
from hub.integrations import local_reviewer
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskRefine, TaskSubmitReview
from hub.services.project_policy import review_dispatch_enabled
from hub.services.model_family import family
from hub.services.review_dispatch import (
    DEEP,
    LITE,
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
    """Подставка провайдера: помнит вызовы, отдаёт заготовленный исход.

    Возвращает ПАРУ (тело, отказ) — шов сместился на create_agent_attempt
    в #1199, потому что путь ревью обязан отличать обрыв связи от отказа
    провайдера. Подставка, оставшаяся на старом шве, молча пропускала бы
    вызовы в настоящий Cursor.
    """

    def __init__(self, result, refusal=None):
        self.result = result
        self.refusal = refusal
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result, self.refusal


def _wire(monkeypatch, recorder: _DispatchRecorder) -> None:
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", recorder)

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
    forge: str = "github",
) -> int:
    areas = ["docs/notes.md"] if areas is None else areas
    pid = await repo.create_project(
        db,
        slug=slug,
        name=slug.title(),
        repo_name=repo_name,
        workspace_path="/tmp/ws",
    )
    if forge != "github":
        # Форж выставляется ДО сдачи намеренно: диспатч случается внутри
        # submit_for_review, и проект, переключённый после, проверял бы не тот
        # момент (#1119).
        await repo.update_project(db, pid, forge=forge)
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


# --- Отчёт по контракту без MCP (#1084) --------------------------------------
#
# У облачного агента Cursor нет инструментов MCP — заголовок с токеном до рана
# не доезжает, и отчёт приходится вытаскивать из текста прогона. Сеть при этом
# работает: ран получил от хаба 401, то есть дошёл и упёрся в отсутствие
# личности. Значит личность надо доставить иначе — одноразовым кодом в промпте,
# который ран меняет на короткую сессию с двумя маршрутами.


_CODE_IN_PROMPT = re.compile(r"AH-[0-9A-HJKMNP-TV-Z]{8}")


def _auth_on(monkeypatch) -> None:
    """Обмен кода — публичный маршрут, но в open mode он отвечает 503: без
    принципалов нет и личности, которую код мог бы нести. _pinned_setup
    закрывает open mode только для middleware, а сам маршрут смотрит на
    config.HUB_TOKENS."""
    from hub.config import TokenIdentity

    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"probe-token": TokenIdentity("probe", "agent")}
    )


async def test_dispatch_mints_a_reviewer_code_and_never_the_token(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#1084): диспатч чеканит код, привязанный к ЭТОЙ задаче, и кладёт
    # его в промпт. Долгоживущего токена там нет никогда, и кода нет в ленте.
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-code"}, "run": {"id": "run-code"}}
    )
    _wire(monkeypatch, recorder)
    _, reviewer_token, _ = await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(client, db, "spike-code", policy={"review": "dispatch"})

    prompt = recorder.calls[-1]["prompt_text"]
    assert reviewer_token not in prompt, "долгоживущий токен в промпт не попадает"

    found = _CODE_IN_PROMPT.search(prompt)
    assert found, "в промпте нет одноразового кода"
    code = found.group(0)

    # Код проверяется ОБМЕНОМ, а не совпадением строки: строка в промпте может
    # быть похожа на код и не быть им.
    redeemed = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert redeemed.status_code == 200, redeemed.text
    body = redeemed.json()
    assert body["kind"] == "reviewer"
    assert body["bound_task_id"] == task_id

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert all(code not in (u["content"] or "") for u in updates), "код утёк в ленту"


async def test_contract_report_wins_and_text_recovery_remains(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#1084): отчёт, сданный сессией кода, записан как контрактный —
    # происхождение mcp, не run_text, — привязан к текущему поколению и
    # сходится с пином принципала своего диспатча. Восстановление из текста
    # при этом остаётся: оно проверяется соседними тестами файла и не должно
    # перезаписывать уже принятый контрактный отчёт.
    recorder = _DispatchRecorder({"agent": {"id": "bc-ctr"}, "run": {"id": "run-ctr"}})
    _wire(monkeypatch, recorder)
    expected_pid, _, _ = await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(
        client, db, "spike-contract", policy={"review": "dispatch"}
    )

    code = _CODE_IN_PROMPT.search(recorder.calls[-1]["prompt_text"]).group(0)
    session = (
        await client.post(
            "/api/auth/chat-pair/redeem",
            json={"code": code},
            headers={"x-forwarded-for": "203.0.113.10"},
        )
    ).json()
    headers = {"Authorization": f"Bearer {session['token']}"}

    brief = await client.get(f"/api/tasks/{task_id}/review-brief", headers=headers)
    assert brief.status_code == 200, brief.text

    filed = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "raw_count": 2,
            "incomplete": False,
            "harness_skill": "multi-agent-review",
            "findings_confirmed": [],
            "findings_rejected": [],
        },
        headers=headers,
    )
    assert filed.status_code == 200, filed.text

    row = dict(await repo.get_latest_machine_review(db, task_id))
    # Происхождение живёт в orchestrator: восстановленный из текста отчёт
    # несёт префикс "cursor-cloud-result:", сданный по контракту — нет.
    assert not str(row["orchestrator"]).startswith("cursor-cloud-result"), (
        "контрактный отчёт не должен помечаться как восстановленный из текста"
    )
    assert row["submission_generation"] == 1
    assert not row["self_reviewed"], "ревьюер — не исполнитель задачи"
    dispatch = await _any_dispatch_row(db, task_id)
    assert dispatch["reviewer_principal_id"] == expected_pid == row["principal_id"]


async def test_prompt_names_the_path_the_run_can_actually_take(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1084: промпт не должен звать в MCP, которого у рана нет. Пока код
    # выписан, основной путь — HTTP, и обзор ревью тоже читается по HTTP:
    # иначе за ним пойдут в несуществующий инструмент, а код живёт минуты.
    recorder = _DispatchRecorder({"agent": {"id": "bc-p1"}, "run": {"id": "run-p1"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    await _submitted(client, db, "spike-prompt-http", policy={"review": "dispatch"})

    with_code = recorder.calls[-1]["prompt_text"]
    assert _CODE_IN_PROMPT.search(with_code), "код в промпте — предпосылка теста"
    assert "/review-brief" in with_code, "обзор ревью тоже по HTTP"
    assert "Основной путь — HTTP" in with_code
    assert "сдай hub_submit_machine_review, это основной путь" not in with_code
    assert "haiplane-review" in with_code, "запасной блок остаётся при любом пути"

    # Без кода (нет ревьюер-принципала) формулировка прежняя: правка условная,
    # а не вычёркивание MCP отовсюду — Cursor может починить доставку.
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "no-such-token")
    await _submitted(client, db, "spike-prompt-mcp", policy={"review": "dispatch"})
    without_code = recorder.calls[-1]["prompt_text"]
    assert not _CODE_IN_PROMPT.search(without_code)
    assert "сдай hub_submit_machine_review, это основной путь" in without_code


async def test_dispatch_pins_the_generation_it_minted_for(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # #1084, находка ревью: пин проверялся ТОЛЬКО на кодах, вычеканенных
    # руками с bound_generation. Диспатч его не передавал, поэтому в проде
    # колонка была NULL, сравнение в обмене пропускалось, сессия несла None,
    # и проверка на приёме была ложной — вся защита оказывалась мёртвой при
    # зелёных тестах. Этот тест идёт ЧЕРЕЗ ДИСПАТЧ и потому её сторожит.
    recorder = _DispatchRecorder({"agent": {"id": "bc-gen"}, "run": {"id": "run-gen"}})
    _wire(monkeypatch, recorder)
    await _pinned_setup(db, monkeypatch)
    _auth_on(monkeypatch)
    task_id = await _submitted(
        client, db, "spike-gen-pin", policy={"review": "dispatch"}
    )

    code = _CODE_IN_PROMPT.search(recorder.calls[-1]["prompt_text"]).group(0)
    rows = await db.execute_fetchall(
        "SELECT bound_generation FROM chat_pair_codes WHERE kind='reviewer'"
    )
    assert [dict(r)["bound_generation"] for r in rows] == [1], "диспатч обязан пинить"

    # Работа пересдана, пока прогон жив: статус остаётся review, меняется сдача.
    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await db.commit()

    refused = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.11"},
    )
    assert refused.status_code == 401, refused.text


async def test_cloud_review_never_calls_cursor_for_a_gitverse_project(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1119): отказ проверяется ПОВЕДЕНИЕМ, а не формой константы.

    Первая редакция этого критерия сверяла ``CLOUD_REVIEW_FORGES == ("github",)``
    — то есть читала объявление вслух. Такой тест переживает удаление самой
    проверки из ``maybe_dispatch_review`` и остаётся зелёным, а платит за это
    оплаченный прогон облачного агента в пустоту (найдено ревью, отчёт #201).

    Здесь Cursor замокан и СЧИТАЕТ вызовы: единственное доказательство, что до
    него не дошли, — пустой список.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gv"}, "run": {"id": "r-gv"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-gitverse",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    assert recorder.calls == [], (
        "облачный ревьюер не должен быть вызван для GitVerse-проекта: "
        "измерено 31.08.2026, что тот же запрос с адресом GitVerse даёт 400 и "
        "500, и ни один ответ не называет причину"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "отказ ревьюера не должен ломать сдачу"

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("gitverse" in a for a in alerts), (
        "карточка обязана назвать ФОРЖ как причину: молчаливый пропуск "
        "неотличим от «ревьюер не настроен» (#498)"
    )


async def test_the_same_submission_on_github_does_reach_cursor(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Контроль к предыдущему: тест обязан уметь падать.

    Без этой пары «вызовов ноль» доказывало бы что угодно — например, что
    диспатч не сработал по совсем другой причине и форж тут ни при чём.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gh"}, "run": {"id": "r-gh"}})
    _wire(monkeypatch, recorder)

    await _submitted(
        client,
        db,
        "spike-github-control",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="github",
    )

    assert len(recorder.calls) == 1, (
        "при том же наборе условий и форже github вызов обязан состояться — "
        "иначе предыдущий тест зелен по посторонней причине"
    )


# ---------------------------------------------------------------------------
# #1152 — второй прогон на том же коде не покупается
# ---------------------------------------------------------------------------


async def _report_on_current(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    incomplete: bool = False,
    self_reviewed: bool = False,
) -> int:
    """Отчёт машинного ревью на текущую генерацию задачи."""
    task = dict(await repo.get_task(db, task_id))
    return await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=int(task["submission_generation"] or 0),
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=3,
        findings_confirmed=json.dumps([]),
        incomplete=incomplete,
        self_reviewed=self_reviewed,
        submitted_by="cursor-cloud-reviewer",
    )


async def test_same_sha_buys_no_second_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1152): код уже прочитан — второй прогон не заказывается.

    Измерено на живой базе прода: 8 дублей из 87 прогонов, порядка 15M
    токенов провайдера. Пересдача на неизменившемся sha — ровно тот путь,
    которым они возникали: генерация новая, дифф прежний.

    Проверяется ОТСУТСТВИЕ строки в review_dispatches, а не отсутствие
    отчёта: заказ и есть оплата.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-same-sha")
    assert len(recorder.calls) == 1, "первая сдача ревью получает"
    review_id = await _report_on_current(db, task_id)
    await db.commit()

    # Пересдача НА ТОМ ЖЕ коммите: поколение растёт, вершина та же.
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 1, (
        "второй прогон на том же sha не заказывается — он вернул бы то же "
        "чтение за те же деньги"
    )
    rows = await db.execute_fetchall(
        "SELECT id FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert len(rows) == 1, "заказа не появляется: заказ и есть оплата"
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any(f"отчёт #{review_id}" in c for c in updates), (
        "отказ обязан назвать отчёт, который уже покрывает этот код: "
        "молчаливый отказ неотличим от сломавшегося диспетчера"
    )


async def test_a_real_resubmission_still_gets_reviewed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1152): новый код ревью ПОЛУЧАЕТ, и тот же код без отчёта тоже.

    Ложный отказ здесь дороже лишнего прогона: сдача, оставшаяся без
    отчёта, уйдёт к человеку вслепую или встанет вовсе. Поэтому зеркало
    двустороннее — сдвинулась вершина, и отдельно случай «тот же sha, но
    отчёта по нему нет».
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)

    # Тот же sha, но отчёта нет вовсе — сравнивать не с чем, прогон нужен.
    fresh = await _submitted(client, db, "spike-no-report")
    assert len(recorder.calls) == 1
    await services.submit_for_review(
        db, fresh, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 2, (
        "без отчёта по этому sha отказывать не за что: незнание не есть совпадение"
    )

    # Отчёт есть, но вершина сдвинулась — это новая работа.
    moved = await _submitted(client, db, "spike-moved")
    assert len(recorder.calls) == 3
    await _report_on_current(db, moved)
    await db.commit()
    plugins.git_ops = _PinnedGitOps("b" * 40, ["docs/notes.md"])
    await services.submit_for_review(
        db, moved, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 4, "новый sha — новая работа, ревью заказывается"


async def test_a_submission_without_a_pinned_sha_is_not_a_match(
    db: aiosqlite.Connection,
):
    """Незакреплённый sha — «сравнивать нечего», а не «совпало».

    Ветка иначе не исполняется ни одним тестом: живая сдача всегда
    закрепляет вершину. Но незнание не есть совпадение (#762), и цена
    ошибки здесь несимметрична — ложный отказ отнимает у сдачи отчёт, а
    лишний прогон стоит только денег.
    """
    from hub.services.review_dispatch import _report_already_covers_this_sha

    assert (
        await _report_already_covers_this_sha(
            db, {"id": 1, "submission_generation": 2, "submission_sha": ""}
        )
        is None
    )
    assert (
        await _report_already_covers_this_sha(
            db, {"id": 1, "submission_generation": 2, "submission_sha": "   "}
        )
        is None
    )


async def test_an_incomplete_report_does_not_lock_the_sha(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполный отчёт не есть прочитанный код (#879).

    Найдено кросс-модельным ревью первой сдачи, и находка бьёт в саму
    задачу: правило, написанное ради экономии прогонов, запирало добор
    непрочитанного. Отчёт с incomplete=true САМ говорит, что дочитал не
    всё, — назвать его чтением значит поверить утверждению, которое он о
    себе опровергает.

    Направление ошибки то же, что и во всём правиле: лишний прогон стоит
    денег, пропущенный — сдачи без ревью.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-incomplete-sha")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id, incomplete=True)
    await db.commit()

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 2, (
        "отчёт, объявивший себя неполным, покрытием не является — иначе "
        "правило запирает лестницу добора #879"
    )


async def test_a_self_report_does_not_cancel_the_independent_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отчёт исполнителя о себе не отменяет ревьюера со стороны.

    Найдено кросс-модельным ревью. Тот же автор уже однажды закрыл чужой
    диспетчер как выполненный своим параллельным отчётом (#1011, #1025) —
    здесь он закрывал бы его ещё до старта, и «код прочитан» означало бы
    «автор прочитал свой код». Независимость — весь смысл кросс-модельного
    контура, и правило экономии не должно её покупать.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-self-report")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id, self_reviewed=True)
    await db.commit()

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 2, (
        "самоотчёт не есть независимое чтение — он не может отменить "
        "кросс-модельного ревьюера"
    )


async def test_the_refusal_is_as_loud_as_the_other_dispatcher_refusals(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Отказ пишется алертом, как остальные отказы диспетчера.

    Найдено кросс-модельным ревью: я написал его как status, то есть тише
    соседей. Отказ, который тише остальных, читается как «ничего не
    произошло» ровно там, где ревьюера не позвали, — а не позвать
    ревьюера это событие, а не отсутствие события.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-refusal-loud")
    review_id = await _report_on_current(db, task_id)
    await db.commit()

    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    kinds = {
        dict(u)["kind"]
        for u in await repo.get_task_updates(db, task_id)
        if f"отчёт #{review_id}" in dict(u)["content"]
    }
    assert kinds == {"alert"}, f"отказ обязан быть слышен как алерт: {kinds}"


async def test_the_top_up_is_not_stopped_by_the_same_sha_guard(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Добор лестницы #879 проверку новизны не проходит и не должен.

    Найдено кросс-модельным ревью, и это ВТОРОЙ раз, когда правило экономии
    мешало добору — первый был про неполный отчёт, этот про принудительный
    профиль. Один и тот же узел с двух сторон.

    Причина в том, что вопросы разные. Страж спрашивает «читали ли уже этот
    код», и на новую сдачу это верный вопрос. Добор спрашивает «дочитал ли
    НАШ прогон», и отчёт чужой генерации на том же sha про это не знает
    ничего.

    Проверяется РАЗНИЦА ИСХОДОВ ИЗ ОДНОГО СОСТОЯНИЯ: при совпавшем sha
    обычный заказ отказывает, а добор проходит. Тест зовёт точку входа
    напрямую, а не через естественную последовательность: чтобы страж
    встретил добор, полный отчёт чужой генерации должен появиться МЕЖДУ
    прогоном этой генерации и его добором, и такую гонку я собрать не смог.
    Правило от этого не зависит — оно про то, какой вопрос кому задан.
    """
    from hub.services.review_dispatch import DEEP, maybe_dispatch_review

    recorder = _DispatchRecorder({"agent": {"id": "bc-1"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup-guard")
    assert len(recorder.calls) == 1
    await _report_on_current(db, task_id)
    await db.commit()

    # Пересдача НА ТОМ ЖЕ коммите: страж срабатывает, заказа нет.
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )
    assert len(recorder.calls) == 1, "обычный заказ на прочитанном коде отказывает"

    # То же состояние, тот же sha — но это добор.
    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert dispatched, (
        "добор обязан пройти там, где обычный заказ отказал: у него другой "
        "вопрос и свой потолок (REVIEW_LADDER_MAX_STEPS)"
    )
    assert len(recorder.calls) == 2
    assert (await _dispatch_row(db, task_id))["profile"] == DEEP


async def test_the_top_up_still_asks_the_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Выключенный контур выключен и для лестницы.

    Мутация, выжившая при первом заходе: я написал в докстринге, что добор
    спрашивает политику, и не проверил этого. Освобождение от проверки
    новизны легко расползается на соседний отказ, стоящий в той же
    функции, — и тогда лестница покупала бы прогоны на проекте, который
    ревью вообще не заказывает.

    Разница между двумя отказами именно в том, на какой вопрос они
    отвечают: «дочитал ли наш прогон» у добора свой, а «просят ли здесь
    ревью» — общий для всех, и лестница из него не выведена.
    """
    from hub.services.review_dispatch import DEEP, maybe_dispatch_review

    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    task_id = await _submitted(client, db, "spike-topup-off", policy={"review": "off"})
    assert recorder.calls == [], "контур выключен — обычного заказа нет"

    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert not dispatched, (
        "добор не обходит выключённый контур: освобождение касается только "
        "проверки новизны"
    )
    assert recorder.calls == []


# --- Локальный ревьюер: второй способ добыть отчёт (#1180) --------------------
#
# Облачный агент Cursor принимает только GitHub (измерено 31.08.2026), поэтому
# на GitVerse независимого машинного ревью не бывает вовсе: отчёт может подать
# только тот, кто делал работу, и гейт его не засчитывает при
# REVIEW_SELF_APPROVE=forbid. Наблюдено на живой задаче #1128 — настоящий
# прогон с семью находками не был засчитан, задача простояла девять часов.
#
# Здесь проверяется ПОВЕДЕНИЕ, а не форма настроек: вместо агентского CLI
# запускается python-заглушка, и всё, что тесты утверждают, они утверждают по
# строкам в базе, по ленте задачи и по тому, что заглушка увидела о себе сама.

_LOCAL_REPORT = {
    "harness_skill": "lite-diff-review",
    "harness_version": 8,
    "raw_count": 3,
    "findings_confirmed": [],
    "findings_rejected": [],
    "incomplete": False,
    "unresolved": [],
    "lost_dimensions": [],
    "agent_count": 4,
    "tokens_spent": 1_500_000,
    "duration_ms": 61_000,
    "orchestrator": "local-stub",
    "model": "grok-4.6",
}


def _scratch(tmp_path) -> str:
    """Каталог прогонов, как его требует выкат: существует и setgid.

    Тесты обязаны описывать прод, а не удобство: на проде каталог создаёт
    оператор с группой, общей у хаба и ревьюера, и хаб отказывается работать
    без setgid — без него подкаталог унаследовал бы основную группу хаба.
    """
    import os

    path = tmp_path / "scratch"
    path.mkdir(exist_ok=True)
    os.chmod(path, 0o2770)
    return str(path)


def _stub_reviewer(monkeypatch, tmp_path, script: str) -> None:
    """Локальный ревьюер = python-заглушка под настоящим префиксом.

    Префикс здесь — системный ``env``: он ничего не изолирует, и в этом весь
    смысл. Тест не может завести на машине разработчика второго unix-
    пользователя, но может доказать, что префикс ДЕЙСТВИТЕЛЬНО применяется к
    командной строке: заглушка видит себя запущенной через него. Настоящая
    изоляция — свойство выката (deploy/LOCAL-REVIEW.md), и её проверка
    попыткой названа в AC-2 ручной честно, а не подменена этим тестом.
    """
    import shlex
    import shutil
    import sys

    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_CMD", shlex.join([sys.executable, "-c", script])
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))


async def _local_principal(db, monkeypatch) -> int:
    """Принципал локального ревьюера — тот, чей токен подпишет отчёт."""
    monkeypatch.setattr(hub_auth, "_is_open_mode", lambda: False)
    pid, token = await _agent_key(db, "local-reviewer")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", token)
    return pid


def _reporting_stub(report: dict | None = None) -> str:
    """Заглушка, оставляющая отчёт блоком в собственном выводе."""
    payload = json.dumps(report or _LOCAL_REPORT, ensure_ascii=False)
    return (
        "import sys\n"
        "sys.stdin.read()\n"
        "print('```haiplane-review')\n"
        f"print({payload!r})\n"
        "print('```')\n"
    )


async def test_local_review_lands_an_independent_report(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1180): на форже без облака отчёт всё равно появляется, и он чужой.

    Проверяется СТРОКОЙ В БАЗЕ, а не намерением запуска: отчёт текущего
    поколения, ``self_reviewed=0``, непустые agent_count и tokens_spent. Это и
    есть то, чего на GitVerse не бывало вовсе.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-nope"}, "run": {"id": "r-nope"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client,
        db,
        "spike-local-report",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    assert recorder.calls == [], "облако на этом форже не зовут (#1119)"
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, "прогон обязан оставить ровно один отчёт"
    report = reports[0]
    assert report["principal_id"] == reviewer_pid, (
        "отчёт принадлежит принципалу ревьюера — независимость держит токен, "
        "а не машина (#728)"
    )
    assert not report["self_reviewed"], (
        "ровно это и не получалось на #1128: отчёт автора гейт не засчитывает "
        "при REVIEW_SELF_APPROVE=forbid"
    )
    assert report["agent_count"] == 4 and report["tokens_spent"] == 1_500_000, (
        "пустые метрики — признак отчёта без исполнения (харнесс v8)"
    )
    dispatch = dict(
        (
            await db.execute_fetchall(
                "SELECT * FROM review_dispatches WHERE task_id=?", (task_id,)
            )
        )[-1]
    )
    assert dispatch["channel"] == "local" and dispatch["status"] == "done"


async def test_the_local_reviewer_gets_neither_hub_secrets_nor_the_clone(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2, машинная часть: процесс не получает ни секретов, ни клона.

    Проверяется ПОПЫТКОЙ, а не чтением конфига: секрет кладётся в окружение
    хаба по-настоящему, заглушка честно печатает про себя всё, что видит, и
    утверждение делается по её собственному свидетельству. Настройка, о
    которой только заявлено, защитой не является.

    Ручная половина AC-2 — попытка прочитать secrets.env и записать в рабочий
    клон ПОД ПОЛЬЗОВАТЕЛЕМ ревьюера — этим тестом не закрывается и за
    закрытую не выдаётся: на машине разработчика второго пользователя нет.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    probe = tmp_path / "probe.json"
    recorder = _DispatchRecorder({"agent": {"id": "bc-x"}, "run": {"id": "r-x"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "secret-cursor-key")
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "secret-hub-token")
    monkeypatch.setenv("HUB_ENV_CANARY", "canary-value")
    _stub_reviewer(
        monkeypatch,
        tmp_path,
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        f"open({str(probe)!r}, 'w').write(json.dumps("
        "{'env': dict(os.environ), 'cwd': os.getcwd(), 'argv': sys.argv, "
        "'prompt': prompt}))\n",
    )

    await _submitted(
        client,
        db,
        "spike-local-env",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()

    seen = json.loads(probe.read_text())
    assert "CURSOR_API_KEY" not in seen["env"], (
        "ключ провайдера лежит в окружении хаба и не имеет права уехать в "
        "чужой процесс: окружение собирается белым списком, а не копией"
    )
    assert "HAIPLANE_HUB_TOKEN" not in seen["env"]
    assert "secret-cursor-key" not in json.dumps(seen["env"]), (
        "проверка по ЗНАЧЕНИЮ, а не только по имени: переименованный секрет "
        "утёк бы мимо проверки по ключу"
    )
    assert "canary-value" not in json.dumps(seen["env"]), (
        "канарейка положена в окружение хаба специально: если она доехала, "
        "значит окружение копируется, а не собирается"
    )
    # Утверждать «в ребёнке ровно наш набор» нельзя, и это не придирка:
    # интерпретатор ребёнка и macOS заводят себе переменные сами (LC_CTYPE,
    # __CF_USER_TEXT_ENCODING), причём с теми же значениями, что у родителя, —
    # обе стороны вычислили их одинаково, а не унаследовали. Поэтому что хаб
    # ПЕРЕДАЁТ, спрашивается у сборщика окружения, а что доехало — у ребёнка.
    passed = local_reviewer._clean_env("/scratch/run")
    assert set(passed) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "HOME", "TMPDIR"}, (
        f"передаётся только названный список, а не {sorted(passed)}"
    )
    assert passed["HOME"] == passed["TMPDIR"] == "/scratch/run"
    assert seen["env"]["HOME"] == seen["cwd"], "дом ревьюера — каталог прогона"
    assert seen["cwd"].startswith(str(tmp_path / "scratch")), (
        "работа идёт в одноразовом каталоге, а не в клоне проекта"
    )
    assert not any("/tmp/ws" in str(a) for a in seen["argv"]), (
        "путь к рабочему клону проекта процессу не передаётся вовсе"
    )
    assert "не коммить" in seen["prompt"], "промт тот же, что уходит в облако"
    assert not any("haiplane-review" in str(a) for a in seen["argv"]), (
        "промт уходит в stdin: аргументы видны в ps, а промт несёт "
        "одноразовый код доступа к хабу"
    )
    assert not (tmp_path / "scratch").exists() or not list(
        (tmp_path / "scratch").iterdir()
    ), "каталог прогона одноразовый — после прогона от него ничего не остаётся"


async def test_a_dead_local_reviewer_names_its_cause(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-3: зависший прогон снят, хаб жив, причина названа.

    «Ревью не состоялось» и «ревью ничего не нашло» — разные исходы (#419,
    #725). Молчаливый пропуск здесь означал бы, что вердикт выносится по
    отсутствию отчёта, принятому за чистоту.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-h"}, "run": {"id": "r-h"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import time\ntime.sleep(120)\n")
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", 1)

    task_id = await _submitted(
        client,
        db,
        "spike-local-hang",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("снято по таймауту" in a for a in alerts), (
        f"причина обязана быть названа, а не выведена читателем: {alerts}"
    )
    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed"
    _, probe_token = await _agent_key(db, "liveness-probe")
    alive = await client.get(
        f"/api/tasks/{task_id}", headers={"Authorization": f"Bearer {probe_token}"}
    )
    assert alive.status_code == 200, "снятый ревьюер не трогает доступность хаба"


async def test_local_review_obeys_policy_and_cost_ceiling(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4: без просьбы политики и сверх потолка прогона нет, и это сказано.

    Потолок проверяется на ДОБОРЕ (#879) — единственном месте, где второй
    прогон по той же задаче вообще возможен: проверка новизны отказала бы
    раньше и по другой причине, и тест тогда был бы зелен не за то.
    """
    from hub.services.review_dispatch import maybe_dispatch_review, wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-c"}, "run": {"id": "r-c"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)

    runs: list[str] = []
    real_run = local_reviewer.run_review

    async def _counting(prompt, *, timeout=None):
        runs.append(prompt)
        return await real_run(prompt, timeout=timeout)

    monkeypatch.setattr(local_reviewer, "run_review", _counting)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    off_id = await _submitted(
        client,
        db,
        "spike-local-off",
        policy={"review": "off"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    assert runs == [], "политика ревью не просила — прогона нет"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (off_id,)
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-ceiling",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()
    assert len(runs) == 1, "первый прогон состоялся и стоил 1.5M токенов"

    monkeypatch.setattr(config, "LOCAL_REVIEW_TOKEN_CEILING", 1_000_000)
    dispatched = await maybe_dispatch_review(db, task_id, force_profile=DEEP)

    assert not dispatched and len(runs) == 1, "сверх потолка прогон не покупается"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("потолок стоимости исчерпан" in a for a in alerts), alerts
    assert any("1500000" in a and "1000000" in a for a in alerts), (
        "названы обе величины: потраченное и потолок — иначе причину нельзя "
        "проверить, не залезая в конфиг"
    )


async def test_github_still_goes_to_the_cloud_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-5: на GitHub облачный путь остаётся первым и единственным.

    Контрольный тест ко всем остальным: локальный путь настроен полностью, и
    именно поэтому его молчание здесь что-то значит.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-gh"}, "run": {"id": "r-gh"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    runs: list[str] = []

    async def _never(prompt, *, timeout=None):
        runs.append(prompt)
        return None

    monkeypatch.setattr(local_reviewer, "run_review", _never)

    task_id = await _submitted(
        client, db, "spike-github-stays", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "GitHub уходит в облако, как и раньше"
    assert runs == [], "локальный прогон на GitHub не запускается"
    rows = await db.execute_fetchall(
        "SELECT channel FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["channel"] == "cloud"


async def test_a_local_run_lost_to_a_restart_fails_with_its_own_cause(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Прогон, начатый другим процессом хаба, не висит активным вечно.

    И, что важнее формы отказа, свип НЕ идёт за его судьбой в Cursor:
    облачный API про локальный прогон знает только то, что такого агента у
    него нет, — то есть вернул бы ложную причину. Отличать канал по строке
    базы, а не по виду идентификатора, только ради этого и стоило.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, "import sys\nsys.stdin.read()\n")

    task_id = await _submitted(
        client,
        db,
        "spike-local-restart",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    # Прогон закрылся своей корутиной; возвращаем строку в состояние
    # «активна и старше grace» — так она выглядит после перезапуска хаба.
    await db.execute(
        "UPDATE review_dispatches SET status='active', "
        "created_at = datetime('now', '-60 minutes') WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    asked: list[str] = []

    async def _get_run(agent_id, run_id):
        asked.append(agent_id)
        return {"id": run_id, "status": "FINISHED"}

    monkeypatch.setattr(cursor_cloud, "get_run", _get_run)

    await sweep_review_dispatches(db)

    assert asked == [], "про локальный прогон облачный API не спрашивают"
    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed"
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("потеряно" in a and "перезапуска" in a for a in alerts), alerts


# --- Находки ревью по сдаче #1 (отчёт #258) ----------------------------------
#
# Шесть подтверждённых находок про этот же локальный путь. Тесты ниже названы
# по дефекту, а не по фиксу: каждый обязан падать на коде ДО правки, иначе он
# не ловит класс, ради которого написан.


async def test_the_local_top_up_keeps_the_profile_it_was_ordered_with(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 7ed386a8: добор лестницы на не-GitHub снова покупал lite.

    maybe_dispatch_review получал force_profile=DEEP, а до prepare_review_order
    он не доезжал — локальный путь выбирал профиль заново и заказывал второй
    однопроходный прогон вместо харнесса. Молча и за деньги: в карточке стоит
    «профиль lite», как будто так и заказывали.
    """
    from hub.services.review_dispatch import (
        DEEP,
        maybe_dispatch_review,
        wait_for_local_runs,
    )

    recorder = _DispatchRecorder({"agent": {"id": "bc-tu"}, "run": {"id": "r-tu"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(
        monkeypatch, tmp_path, _reporting_stub({**_LOCAL_REPORT, "tokens_spent": 1000})
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-topup",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()
    await db.commit()

    assert await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    await wait_for_local_runs()
    await db.commit()

    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT profile, channel FROM review_dispatches WHERE task_id=? "
            "ORDER BY id",
            (task_id,),
        )
    ]
    assert len(rows) == 2 and rows[1]["channel"] == "local"
    assert rows[1]["profile"] == DEEP, (
        "добор заказан deep — локальный путь обязан исполнить заказанное, а не "
        f"выбрать профиль заново: {rows}"
    )


async def test_the_timed_out_reviewer_is_actually_dead(monkeypatch, tmp_path):
    """Находка aa620d54: AC-3 проверял ТЕКСТ в ленте, а не смерть процесса.

    kill_process_group никогда не бросает, поэтому прежний тест оставался
    зелёным и при живом ревьюере — он читал только слова хаба о самом себе.
    Здесь доказательство внешнее: полезная нагрузка пишет маркер ПОСЛЕ
    таймаута, и файла быть не должно.

    Хвост `; :` не украшение (#544): одна простая команда была бы заменена
    шеллом через exec, полезная нагрузка стала бы тем самым pid, который мы
    сигналим, и тест прошёл бы на macOS при живом процессе на Linux.
    Составная команда заставляет sh форкнуться — до внука дотягивается только
    убийство группы.
    """
    import shlex
    import shutil
    import sys

    marker = tmp_path / "still_alive"
    payload = (
        f"{sys.executable} -c "
        + shlex.quote(
            "import time, pathlib, sys; sys.stdin.read(); time.sleep(2.5); "
            f"pathlib.Path({str(marker)!r}).write_text('x')"
        )
        + "; :"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_CMD", shlex.join(["/bin/sh", "-c", payload])
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=1)

    assert run is not None and run.timed_out
    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "ревьюер пережил собственный таймаут: хаб написал в ленту, что снял "
        "процесс, и это было бы неправдой"
    )


async def test_stopping_the_hub_kills_the_local_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка d478b896: отмена проходила мимо перехвата и оставляла сироту.

    CancelledError наследует BaseException, поэтому except на таймаут его не
    видел, а wait_for_local_runs, объявленная «для остановки хаба», из
    lifespan никем не звалась. Проверяется тем же внешним маркером.
    """
    from hub.services.review_dispatch import cancel_local_runs

    import shlex
    import shutil
    import sys

    marker = tmp_path / "outlived_the_hub"
    payload = (
        f"{sys.executable} -c "
        + shlex.quote(
            "import time, pathlib, sys; sys.stdin.read(); time.sleep(2.5); "
            f"pathlib.Path({str(marker)!r}).write_text('x')"
        )
        + "; :"
    )
    recorder = _DispatchRecorder({"agent": {"id": "bc-st"}, "run": {"id": "r-st"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_CMD", shlex.join(["/bin/sh", "-c", payload])
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))

    await _submitted(
        client,
        db,
        "spike-local-stop",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await asyncio.sleep(0.3)

    await cancel_local_runs()

    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "хаб остановился, а ревьюер продолжил работать сиротой — и мог бы ещё "
        "прислать отчёт по прогону, за которым больше некому смотреть"
    )


async def test_a_chatty_reviewer_does_not_grow_the_hub(monkeypatch, tmp_path):
    """Находка 30a65c79: лимит применялся ПОСЛЕ чтения всего вывода в память.

    communicate() читает оба потока до EOF, и OUTPUT_CAP резал уже собранную
    строку — то есть не ограничивал ничего. Лимит памяти при этом стоит на
    слайсе ревьюера, а росла память ХАБА.
    """
    import shlex
    import shutil
    import sys

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [sys.executable, "-c", "import sys; sys.stdin.read(); print('x' * 500000)"]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=30)

    assert run is not None and not run.timed_out
    assert len(run.output) <= 1000, "в памяти остаётся только хвост под лимитом"
    assert run.dropped > 400_000, (
        "выброшенное считается: «вывод кончился» и «вывод обрезан» — разные "
        "факты, и второй должен быть виден в ленте"
    )


async def test_a_detaching_sandbox_is_refused_by_name(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находки a6aaffbc и 7897d52c: песочница из документа отсоединяла процесс.

    systemd-run без --scope поднимает transient service: родитель CLI — PID 1,
    таймаут его не снимет, а cwd и белый список окружения осядут на клиенте.
    Хаб не имеет права запускать прогон, снять который он не сможет, — и
    обязан назвать причину, а не промолчать.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-ds"}, "run": {"id": "r-ds"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    runs: list[str] = []

    async def _never(prompt, *, timeout=None):
        runs.append(prompt)
        return None

    monkeypatch.setattr(local_reviewer, "run_review", _never)
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --quiet --pipe --uid=haiplane-reviewer --",
    )

    task_id = await _submitted(
        client,
        db,
        "spike-local-detach",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await wait_for_local_runs()

    assert runs == [], "прогон, который нельзя снять, не запускается вовсе"
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("--scope" in a for a in alerts), (
        f"отказ обязан назвать недостающий флаг, а не «песочница неверна»: {alerts}"
    )


# --- Находки ревью по сдаче #3 (отчёт #265) ----------------------------------
#
# Первая из них — регрессия, которую открыл фикс предыдущей: пока песочница
# отсоединяла процесс, права каталога-однодневки никого не задевали, а с
# --scope они стали решающими. Ровно то, что харнесс называет
# fix-induced-regression.


async def test_the_scratch_dir_is_writable_by_the_reviewer(monkeypatch, tmp_path):
    """Находка 92eba4f8: mkdtemp даёт 0700, и ревьюеру закрыт его же каталог.

    С --scope cwd, HOME и TMPDIR доезжают до CLI по-настоящему — значит
    каталог, созданный ХАБОМ, должен быть доступен ЧУЖОМУ пользователю по
    общей группе. 0700 владельца-создателя означал бы EACCES на собственный
    рабочий каталог: CLI упал бы, не начав, а в ленте стояло бы «завершилось
    без отчёта» вместо успешного прогона.

    Проверяется правами, снятыми САМИМ процессом со своего cwd, а не
    вычислением по коду: второго unix-пользователя на машине разработчика нет,
    но режим каталога — это ровно то, что решает исход.
    """
    import shlex
    import shutil
    import sys

    probe = tmp_path / "mode.txt"
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdin.read(); "
                f"open({str(probe)!r}, 'w').write(oct(os.stat(os.getcwd()).st_mode & 0o777))",
            ]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=30)

    assert run is not None and not run.timed_out
    assert probe.read_text() == "0o770", (
        "каталог прогона обязан быть доступен группе, общей у хаба и ревьюера: "
        f"получено {probe.read_text()}"
    )


async def test_stopping_the_hub_closes_the_dispatch_row(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 30e3ac32: процесс убивали, а строку прогона не закрывали.

    _supervise_local_run ждал run_review ДО try, поэтому CancelledError
    проходил мимо except Exception и _settle_local_run не вызывался. Итог:
    процесс мёртв, а карточка в настоящем времени говорит «Машинное ревью
    запущено ЛОКАЛЬНО», и свип назовёт это потерей только минут через сорок
    пять. «Ещё идёт» и «снято при остановке» — разные факты.
    """
    from hub.services.review_dispatch import cancel_local_runs

    import shlex
    import shutil
    import sys

    recorder = _DispatchRecorder({"agent": {"id": "bc-sd"}, "run": {"id": "r-sd"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [sys.executable, "-c", "import time, sys; sys.stdin.read(); time.sleep(30)"]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))

    task_id = await _submitted(
        client,
        db,
        "spike-local-shutdown",
        policy={"review": "dispatch"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    await asyncio.sleep(0.3)

    await cancel_local_runs()
    await db.commit()

    rows = await db.execute_fetchall(
        "SELECT status FROM review_dispatches WHERE task_id=?", (task_id,)
    )
    assert dict(rows[-1])["status"] == "failed", (
        "снятый прогон не имеет права остаться активным: активная строка "
        "означает «ревью идёт»"
    )
    alerts = [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if dict(u)["kind"] == "alert"
    ]
    assert any("остановке хаба" in a for a in alerts), (
        f"причина обязана быть названа, а не выведена из тишины: {alerts}"
    )


def test_the_lifespan_cancels_local_runs_on_shutdown():
    """Находка 6e4d7e6b: тест снятия звал функцию, а не путь, которым она живёт.

    Сними две строки из finally в lifespan — и прогоны снова переживут хаб,
    а тест, зовущий cancel_local_runs напрямую, останется зелёным. Здесь
    проверяется именно ВЫЗОВ из lifespan.

    Читается исходник, а не поведение, и это осознанный размен: настоящий
    подъём lifespan открывает боевую базу по HUB_DB_PATH и поднимает
    MCP-транспорт, то есть тест трогал бы установку разработчика. Приём в
    репозитории принятый — так же сверяются с исходником страж имени бренда
    и проверка читателя политики.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "hub" / "app.py"
    tree = ast.parse(source.read_text())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    finalizers = [
        n for node in ast.walk(fn) if isinstance(node, ast.Try) for n in node.finalbody
    ]
    called = {
        n.func.id
        for block in finalizers
        for n in ast.walk(block)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "cancel_local_runs" in called, (
        "остановка хаба обязана снимать локальные прогоны: без вызова в finally "
        "агентский CLI переживёт процесс, который его породил"
    )


async def test_the_collector_keeps_reading_past_the_cap(monkeypatch):
    """Находка cc48162a: тест лимита не отличал чтение чанками от communicate().

    dropped = len(всё) - cap считается и после полного чтения в память, так
    что прежний тест был зелёным и для той реализации, которую находка 30a65c79
    просила убрать. Здесь проверяется САМО поведение читателя: он берёт поток
    по кускам и, перебрав лимит, ПРОДОЛЖАЕТ читать — иначе ребёнок встанет на
    полной трубе и умрёт только по таймауту.
    """

    class _Stream:
        def __init__(self, chunks: list[bytes]):
            self.chunks = chunks
            self.reads = 0

        async def read(self, _n: int) -> bytes:
            self.reads += 1
            return self.chunks.pop(0) if self.chunks else b""

    class _Proc:
        def __init__(self, stream):
            self.stdout = stream
            self.waited = False

        async def wait(self):
            self.waited = True

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 500)
    stream = _Stream([b"a" * 400, b"b" * 400, b"c" * 400])
    proc = _Proc(stream)

    kept, dropped = await local_reviewer._collect(proc)

    assert len(kept) == 500 and kept.endswith(b"b"), "в памяти остаётся ровно лимит"
    assert dropped == 700, f"выброшенное считается поштучно, а не на глаз: {dropped}"
    assert stream.reads >= 4, (
        "читатель обязан вычитать поток до конца кусками, а не остановиться "
        f"на лимите: замолчавший читатель оставляет ребёнка на полной трубе "
        f"(чтений {stream.reads})"
    )
    assert proc.waited, "процесс должен быть дождан, иначе останется зомби"


# --- Находки ревью по сдаче #4 (отчёт #267) ----------------------------------


def test_a_scratch_dir_that_cannot_be_shared_is_refused_by_name(monkeypatch, tmp_path):
    """Неразрешённая 45971e09: 0770 без верной группы — всё тот же отказ.

    Валидатор прав в существе: хаб ставит каталогу прогона 0770, то есть даёт
    доступ ГРУППЕ, а если группа не та, ревьюер получит EACCES — и тест на
    режим этого не увидит, потому что владелец проходит сам. Проверять
    членство чужого пользователя в группе на машине разработчика нельзя, но
    можно проверить то, ОТ ЧЕГО оно зависит, и отказать заранее с названной
    причиной. Три дороги в одно и то же состояние, и все три названы.
    """
    import os

    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/env")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(tmp_path / "missing"))
    assert any("каталога" in r for r in local_reviewer.not_ready()), (
        "каталог, созданный хабом на лету, получит группу хаба — то есть ту, "
        "куда ревьюеру хода нет; это причина, а не мелочь"
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    os.chmod(plain, 0o770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(plain))
    assert any("setgid" in r for r in local_reviewer.not_ready()), (
        "без setgid подкаталог унаследует основную группу хаба, и права 0770 "
        "достанутся не тому"
    )

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(shared))
    assert local_reviewer.not_ready() == [], "правильный каталог претензий не вызывает"

    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=nobody --",
    )
    assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
        "пользователь песочницы вне группы каталога — тот же EACCES, только "
        "предсказуемый заранее"
    )

    # ЧИСЛОВОЙ --uid: systemd-run принимает и его, а getpwnam("1") бросает
    # KeyError — то есть проверка на таком значении молча ничего не проверяла
    # (найдено ревью, неразрешённая 534e16e4). uid 1 есть на обеих системах,
    # где это гоняется, и в группе каталога он не состоит.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/systemd-run --scope --uid=1 --"
    )
    assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
        "числовая форма --uid обязана проверяться так же, как именная: "
        "иначе один синтаксис обходит проверку целиком"
    )

    # Свой uid числом — доступ есть, претензий нет: проверка не должна
    # отказывать всем подряд, иначе она не проверка, а запрет.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={os.getuid()} --",
    )
    assert local_reviewer.not_ready() == [], "владелец каталога проходит по группе"

    # Пользователь, которого в системе нет: «не смогли проверить» — это тоже
    # причина, а не разрешение (неразрешённая 36a63d6b). Пустой not_ready()
    # означает «настроено», и вернуть его, ничего не проверив, значит
    # пообещать работу там, где ревьюер упрётся в EACCES.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=no-such-user-1180 --",
    )
    assert any("не разрешается в системе" in r for r in local_reviewer.not_ready()), (
        "неудача проверки не имеет права читаться как «настроено»"
    )


async def test_the_chatty_reviewer_output_never_lands_in_memory(monkeypatch, tmp_path):
    """Неразрешённая e021e4bf: прежний тест был зелёным и для communicate().

    Валидатор прав: len(output) и dropped сходятся и после полного чтения в
    память, поэтому старая проверка не отличала чтение чанками от «прочитали
    всё и обрезали», а unit-тест на _collect этого пути не видел вовсе.

    Здесь измеряется то, ради чего правка и делалась: пиковая память САМОГО
    процесса хаба. communicate() собрал бы весь вывод одним объектом, чтение
    кусками держит в памяти только лимит.
    """
    import shlex
    import shutil
    import sys
    import tracemalloc

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", shutil.which("env") or "/usr/bin/env"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.read(); sys.stdout.write('x' * 8_000_000)",
            ]
        ),
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    tracemalloc.start()
    try:
        run = await local_reviewer.run_review("промт", timeout=60)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert run is not None and not run.timed_out
    assert run.dropped > 7_000_000, "почти весь вывод обязан быть выброшен"
    assert peak < 2_000_000, (
        f"вывод ревьюера не имеет права оседать в памяти хаба целиком: пик "
        f"{peak} байт при выводе 8 МБ и лимите 1000"
    )


# --- #1199: ответ не дошёл, но агент создан -------------------------------
#
# Измерено 06.09.2026: три «отказа» подряд, и по каждому у провайдера нашёлся
# живой оплаченный агент (08:49:06Z, 08:50:53Z, 11:28:05Z). Хаб при этом
# писал «Cloud Agents API не принял запрос» и уводил вердикт к человеку.
# Наивный повтор покупал бы второго агента каждый раз.

_LOST_ANSWER = cursor_cloud.Refusal(status=0, detail="ReadTimeout: ")
_REAL_REFUSAL = cursor_cloud.Refusal(
    status=400, code="invalid_model", detail="Model is not available"
)


class _Listing:
    """Подставка списка агентов у провайдера.

    Форма ответа — ключ `items` и `nextCursor`, как у настоящего API
    (проверено вызовом 06.09). Выдуманная форма дала бы пустой список, а
    пустой список здесь означает «агента нет» и разрешает купить второго.
    """

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def __call__(self, limit=50, cursor=""):
        self.calls += 1
        if self.pages is None:
            return None
        index = int(cursor or 0)
        if index >= len(self.pages):
            return {"items": [], "nextCursor": ""}
        nxt = str(index + 1) if index + 1 < len(self.pages) else ""
        return {"items": self.pages[index], "nextCursor": nxt}


async def test_an_agent_created_behind_a_lost_answer_is_adopted(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-1: агент подбирается, а второй НЕ покупается.

    Проверяется не только исход, но и число обращений к созданию: подбор
    обязан заменить второй POST, а не сопровождать его.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    marker_seen: list[str] = []

    async def _listing(limit=50, cursor=""):
        # Имя берём из того же запроса, который «не ответил» — так его
        # увидел бы и провайдер.
        marker = recorder.calls[0]["name"]
        marker_seen.append(marker)
        return {"items": [{"id": "bc-adopted", "name": marker}], "nextCursor": ""}

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)

    task_id = await _submitted(client, db, "spike-adopt")

    assert len(recorder.calls) == 1, "второй POST — это второй оплаченный агент"
    assert marker_seen and marker_seen[0] == cursor_cloud.agent_marker(
        "review", task_id, 1
    )
    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-adopted", (
        "подобранный агент записан как исполнитель этого диспетча"
    )


async def test_the_adoption_is_named_not_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: подбор виден человеку и надзору.

    Тихий подбор прячет оплаченный прогон ровно так же, как его прятал
    потерянный идентификатор.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [{"id": "bc-adopted", "name": recorder.calls[0]["name"]}],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-adopt-visible")

    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='status'",
        (task_id,),
    )
    assert any("подобран по метке" in dict(r)["content"] for r in rows), (
        "карточка обязана назвать подбор"
    )
    events = await db.execute_fetchall(
        "SELECT payload FROM events WHERE task_id=? AND kind='review_dispatched'",
        (task_id,),
    )
    assert any(json.loads(dict(e)["payload"]).get("adopted") for e in events), (
        "надзор считает подборы по событию, а не по тексту карточки"
    )


async def test_a_real_refusal_is_neither_reconciled_nor_retried(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-3: ответ БЫЛ и он отрицательный — спрашивать нечего.

    Сверка на настоящем отказе — лишний запрос, а повтор — лишние деньги
    на заведомо тот же исход.
    """
    recorder = _DispatchRecorder(None, refusal=_REAL_REFUSAL)
    _wire(monkeypatch, recorder)
    listing = _Listing([[{"id": "bc-x", "name": "чужой"}]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-real-refusal")

    assert listing.calls == 0, "на настоящем отказе провайдера сверка не нужна"
    assert len(recorder.calls) == 1, "повтор на 400 покупает тот же отказ"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "провайдер отказал" in alerts and "HTTP 400" in alerts
    assert "не принял запрос (бета" not in alerts, (
        "прежний текст утверждал причину, которой не было"
    )


async def test_an_unreadable_reconciliation_never_guesses(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-4: «не смогли спросить» — это не «агента нет».

    Сверка сама оборвалась. Подобрать нельзя: любой похожий агент был бы
    угадан. Состояние называется человеку как есть — правило #762 в его
    исходной форме, отсутствие данных не есть значение.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)
    listing = _Listing(None)  # провайдер не ответил и на список
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-blind")

    assert listing.calls == 1, "спросить обязаны"
    assert not await repo.list_active_review_dispatches(db), (
        "неопознанный агент хуже пропущенного: записывать нечего"
    )
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "ответ провайдера не дошёл" in alerts, "причина названа наблюдённая"
    assert "ReadTimeout" in alerts, "класс исключения доезжает до человека"


async def test_a_similar_agent_is_not_close_enough(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: сравнение имени на РАВЕНСТВО, а не на вхождение.

    Метка соседнего поколения отличается одним символом. Подобрать её
    значило бы отдать сдаче чужого судью — хуже, чем не подобрать никого.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        mine = recorder.calls[0]["name"]
        return {
            "items": [
                {"id": "bc-other-gen", "name": mine + "9"},
                {"id": "bc-prefix", "name": mine[:-1]},
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-similar")

    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert any("не нашлось" in dict(r)["content"] for r in rows)


class _FlakyThenFine:
    """Первый вызов обрывается, второй проходит — самый частый исход."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return None, _LOST_ANSWER
        return {"agent": {"id": "bc-second"}, "run": {"id": "run-2"}}, None


async def test_a_lost_answer_without_an_agent_is_retried_within_a_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199 AC-2: сверка сказала «агента нет» — повторяем, а не сдаёмся.

    Вердикт не уходит к человеку с первого же обрыва. Повтор безопасен
    ИМЕННО потому, что сверка прошла и показала пустоту: без неё он купил
    бы второго агента.
    """
    flaky = _FlakyThenFine()
    _wire(monkeypatch, flaky)
    listing = _Listing([[]])  # спросили, ответ пустой: агента нет
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-retry")

    assert len(flaky.calls) == 2, "ровно один повтор после подтверждённой пустоты"
    assert listing.calls == 1, "спрашиваем перед повтором, а не после"
    assert flaky.calls[0]["name"] == flaky.calls[1]["name"], (
        "повтор идёт под ТОЙ ЖЕ меткой — иначе подобрать его потом нечем"
    )
    rows = await repo.list_active_review_dispatches(db)
    assert len(rows) == 1 and dict(rows[0])["agent_id"] == "bc-second"
    alerts = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert not any("НЕ вызвано" in dict(r)["content"] for r in alerts), (
        "успешный повтор не оставляет жалобы"
    )


async def test_the_retry_stops_at_the_cap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#1199: потолок держит. Иначе это семнадцать минут одной неудачи."""
    always_lost = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, always_lost)
    listing = _Listing([[]])
    monkeypatch.setattr(cursor_cloud, "list_agents", listing)

    task_id = await _submitted(client, db, "spike-cap")

    assert len(always_lost.calls) == 2, "две попытки, не больше"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    assert any("не нашлось за попыток: 2" in dict(r)["content"] for r in rows)


# --- находки ревью №269 ---------------------------------------------------


async def test_a_top_up_does_not_adopt_the_first_reviewer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269 high: добор той же генерации — другой заказ, и метка это знает.

    Лестница (#879) заказывает второго ревьюера на ТУ ЖЕ генерацию. Метка
    без номера попытки совпала бы с первой, и сверка после оборвавшегося
    добора подобрала бы агента дешёвого прогона: второго POST нет, но
    судья чужой, а настоящий остался бы сиротой.
    """
    first = _DispatchRecorder({"agent": {"id": "bc-first"}, "run": {"id": "run-1"}})
    _wire(monkeypatch, first)
    task_id = await _submitted(client, db, "spike-topup-marker")
    assert len(first.calls) == 1
    first_marker = first.calls[0]["name"]

    # Добор: POST обрывается, а в выдаче стоит агент ПЕРВОГО заказа.
    lost = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", lost)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [{"id": "bc-first", "name": first_marker, "latestRunId": "run-1"}],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    await maybe_dispatch_review(db, task_id, force_profile="deep")

    assert lost.calls, "добор вообще пробовал создать агента"
    assert lost.calls[0]["name"] != first_marker, (
        "метка добора обязана отличаться от метки первого заказа"
    )
    rows = await repo.list_active_review_dispatches(db)
    agents = {dict(r)["agent_id"] for r in rows}
    assert agents == {"bc-first"}, (
        "агент первого прогона не должен стать исполнителем добора"
    )


async def test_the_adopted_agent_brings_its_run_id(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269: подбор без run_id — половина потери.

    Без него свип бьёт в /v1/agents/{id}/runs/ с пустым хвостом: не видит
    статус, не восстанавливает отчёт из текста прогона и не ставит расход.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {
            "items": [
                {
                    "id": "bc-adopted",
                    "name": recorder.calls[0]["name"],
                    "latestRunId": "run-adopted",
                }
            ],
            "nextCursor": "",
        }

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    await _submitted(client, db, "spike-adopt-runid")

    row = dict((await repo.list_active_review_dispatches(db))[0])
    assert row["agent_id"] == "bc-adopted"
    assert row["run_id"] == "run-adopted", "идентификатор прогона едет с агентом"


async def test_a_body_of_the_wrong_shape_is_not_an_empty_answer(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """#269: чужая форма ответа — «не смог спросить», а не «агента нет».

    page.get('items') or [] на теле без items давал пустой обход и в конце
    подтверждённую пустоту, которая разрешает купить второго агента. Это
    ровно #762 в новом месте: отсутствие данных снова стало бы значением.
    """
    recorder = _DispatchRecorder(None, refusal=_LOST_ANSWER)
    _wire(monkeypatch, recorder)

    async def _listing(limit=50, cursor=""):
        return {"agents": [], "next": ""}  # схема беты изменилась

    monkeypatch.setattr(cursor_cloud, "list_agents", _listing)
    task_id = await _submitted(client, db, "spike-wrong-shape")

    assert len(recorder.calls) == 1, "на нечитаемой сверке второго POST быть не должно"
    assert not await repo.list_active_review_dispatches(db)
    rows = await db.execute_fetchall(
        "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
        (task_id,),
    )
    alerts = " ".join(dict(r)["content"] for r in rows)
    assert "СПРОСИТЬ" in alerts, "состояние названо как есть, а не как пустота"


# ---------------------------------------------------------------------------
# #1216 — тихий отказ по политике называет себя в карточке
#
# Измерено на #1202 (snip-portal, форж gitverse, gate_policy без ключа review):
# две сдачи подряд встали в review, и в карточке НЕТ НИ ОДНОЙ записи про
# диспетч ревью. Порядок проверок в maybe_dispatch_review отказывает по
# политике раньше, чем управление доходит до развилки по форжу, где стоит
# именованный отказ #1180. Второго читателя не будет ни при каких условиях, а
# карточка об этом молчит — и молчание читается как «ревью не потребовалось».
# ---------------------------------------------------------------------------


#: Собственный словарь диспетчера: чем он открывает вызов и чем — отказ.
#: Фильтровать по одному слову «ревью» нельзя: сдача пишет в карточку и своё
#: («ревью валидно и без PR»), и тогда счётчик считал бы чужие строки.
_DISPATCH_SAYS = (
    "ревью вызвано хабом",
    "ревью НЕ вызвано",
    "ревью запущено ЛОКАЛЬНО",
)


async def _dispatch_notices(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Всё, что карточка говорит о диспетче ревью: и вызовы, и отказы."""
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if any(phrase in dict(u)["content"] for phrase in _DISPATCH_SAYS)
    ]


async def test_a_submission_without_a_reviewer_says_so_on_the_card(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1216): политика не просит диспетча — карточка называет это.

    Политика snip-portal снята с прода 09.09.2026 как есть: dor и verdict
    человеку, ключа review нет вовсе. До правки этот путь не писал ничего, и
    задача стояла в review без второго читателя молча.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1216"}, "run": {"id": "r-1216"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-1216-silent",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    assert recorder.calls == [], "политика не просила диспетча — звать некого"
    notices = await _dispatch_notices(db, task_id)
    assert notices, (
        "карточка обязана сказать, что второго читателя не будет: тишина на "
        "этом месте читается как «ревью не потребовалось» (#1202)"
    )
    said = " ".join(notices)
    assert "политика проекта не просит" in said, (
        "причина называется по существу, а не общими словами: отказала именно "
        "политика проекта"
    )
    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "запись не должна ломать сдачу"


async def test_the_refusal_names_the_missing_settings(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1216): имена недостающих настроек берутся у review_reach.

    Второго источника правды не заводим: тот же текст человек видит в форме
    проекта и в отказе на записи политики (#1188). Сверяем ПОДСТРОКОЙ, а не
    похожестью — расхождение в одном слове означало бы вторую копию знания.
    """
    from hub.services.review_dispatch import review_reach

    recorder = _DispatchRecorder({"agent": {"id": "bc-r"}, "run": {"id": "r-r"}})
    _wire(monkeypatch, recorder)
    # Локальный путь не настроен — ровно состояние прода на 09.09.2026.
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "")

    task_id = await _submitted(
        client,
        db,
        "spike-1216-names",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )

    said = " ".join(await _dispatch_notices(db, task_id))
    reach = await review_reach(db, "gitverse")
    assert not reach.runnable, "предпосылка теста: добыть ревью нечем"
    assert reach.reason in said, (
        "текст про форж и настройки обязан быть ТЕМ ЖЕ, что отдаёт "
        "review_reach: иначе три места объясняют одно состояние тремя словами"
    )
    assert "LOCAL_REVIEW_CMD" in said and "LOCAL_REVIEWER_HUB_TOKEN" in said, (
        "настройки названы по именам (#1083)"
    )
    assert "deploy/LOCAL-REVIEW.md" in said, "назван порядок включения"


async def test_the_notice_is_written_once_per_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1216): свип проходит по сдаче снова — записи не прибавляется.

    Отказ по политике не меняет ни статуса, ни review_job_id, поэтому каждый
    следующий проход входит в ту же ветку. Без дедупа карточка засорялась бы
    одинаковыми алертами до самого вердикта.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-d"}, "run": {"id": "r-d"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client,
        db,
        "spike-1216-dedup",
        policy={"dor": "human", "verdict": "human"},
        repo_name="mrpda/snip-portal",
        forge="gitverse",
    )
    first = len(await _dispatch_notices(db, task_id))
    assert first == 1, "первая сдача оставляет ровно одну запись"

    assert await maybe_dispatch_review(db, task_id) is False
    assert await maybe_dispatch_review(db, task_id) is False

    assert len(await _dispatch_notices(db, task_id)) == 1, (
        "повторные проходы по той же сдаче второй записи не создают"
    )


async def test_a_dispatched_project_gets_no_extra_notice(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-4 (#1216): на проекте с review=dispatch не изменилось ничего.

    Контроль к трём предыдущим: они доказывают появление записи, и без этого
    теста «запись появилась» было бы совместимо с «запись появляется всегда»,
    то есть с засорением карточек работающих проектов.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-ok"}, "run": {"id": "r-ok"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(
        client, db, "spike-1216-working", policy={"review": "dispatch"}
    )

    assert len(recorder.calls) == 1, "ревью вызывается как раньше"
    notices = await _dispatch_notices(db, task_id)
    assert len(notices) == 1 and "вызвано хабом" in notices[0], (
        "единственная запись про ревью — та самая, что была до правки: "
        "новой записи на рабочем пути не появилось"
    )

    # Второй тихий отказ той же функции — проверка новизны (#1152). Он
    # случается на проекте с ВКЛЮЧЁННЫМ диспетчем, то есть проходит ровно
    # через новый помощник, и накрыть его записью «политика не просит
    # диспетча» значило бы сказать неправду: политика как раз просила.
    # Первая редакция этого теста мутацию «снять условие, отделяющее тихий
    # путь от рабочего» ПЕРЕЖИЛА — до сюда она не доставала.
    review_id = await _report_on_current(db, task_id)
    await db.commit()
    await services.submit_for_review(
        db, task_id, TaskSubmitReview(model="claude-fable-5")
    )

    assert len(recorder.calls) == 1, "второй прогон на том же коде не покупается"
    said = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "политика проекта не просит" not in said, (
        "отказала проверка новизны, а не политика: чужая причина в карточке "
        "хуже отсутствующей — по ней пойдут править gate_policy"
    )
    assert f"отчёт #{review_id}" in said, (
        "своя причина у этого отказа осталась на месте (#1152)"
    )
    assert len(await _dispatch_notices(db, task_id)) == 1, (
        "и записей про диспетч по-прежнему одна"
    )


# ---------------------------------------------------------------------------
# #1235 — круг ревью: заходы, где находки ЗАКРЫВАЛИСЬ
# ---------------------------------------------------------------------------


def _confirmed(title: str, category: str, line: int) -> dict:
    """Подтверждённая находка в форме, в которой она лежит в отчёте."""
    return {
        "title": title,
        "severity": "high",
        "category": category,
        "file": "hub/services/probe.py",
        "line": line,
        "start_line": line,
        "end_line": line,
        "locator": "lines",
        "detail": "",
    }


def _unresolved(title: str) -> dict:
    """Неразрешённая находка: категории у неё нет по модели (#1085)."""
    return {"title": title, "why": "адъюдикаторы разошлись"}


async def _generation_with_findings(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    confirmed: list[dict] | None = None,
    unresolved: list[dict] | None = None,
) -> int:
    """Поставить задачу на поколение N и положить на него отчёт."""
    await db.execute(
        "UPDATE tasks SET submission_generation=? WHERE id=?", (generation, task_id)
    )
    review_id = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=len(confirmed or []) + len(unresolved or []),
        findings_confirmed=json.dumps(confirmed or [], ensure_ascii=False),
        unresolved=json.dumps(unresolved or [], ensure_ascii=False),
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    return review_id


async def _author_closed_them(
    db: aiosqlite.Connection,
    task_id: int,
    review_id: int,
    generation: int,
    *,
    confirmed: list[dict],
    unresolved: list[dict],
    outcome_confirmed: str = "fixed",
    outcome_unresolved: str = "real_fixed",
) -> None:
    """Автор отчитался, что находки этого поколения закрыты правкой.

    Ровно тот путь, которым исходы попадают в базу на живой задаче: они
    пишутся на СДАЧЕ СЛЕДУЮЩЕГО поколения против отчёта, который вернул
    работу (lifecycle._step_finding_outcomes), и потому лежат с номером
    поколения ОТЧЁТА, а не новой сдачи.
    """
    from hub.services.finding_identity import finding_uids, unresolved_uids

    for index, (uid, finding) in enumerate(zip(finding_uids(confirmed), confirmed)):
        await repo.upsert_finding_outcome(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=generation,
            finding_uid=uid,
            finding_index=index,
            finding_title=finding["title"],
            outcome=outcome_confirmed,
            note="разобрано опытом",
            linked_task_id=None,
            reported_by="dev-agent",
            finding_kind="confirmed",
        )
    for index, (uid, finding) in enumerate(
        zip(unresolved_uids(unresolved), unresolved)
    ):
        await repo.upsert_finding_outcome(
            db,
            review_id=review_id,
            task_id=task_id,
            submission_generation=generation,
            finding_uid=uid,
            finding_index=index,
            finding_title=finding["title"],
            outcome=outcome_unresolved,
            note="воспроизведено зондом",
            linked_task_id=None,
            reported_by="dev-agent",
            finding_kind="unresolved",
        )
    await db.commit()


async def _circle_notices(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Всё, что карточка сказала о круге."""
    return [
        dict(u)["content"]
        for u in await repo.get_task_updates(db, task_id)
        if "идёт по кругу" in dict(u)["content"]
    ]


async def _walk_the_circle(
    db: aiosqlite.Connection,
    task_id: int,
    layers: list[tuple[list[dict], list[dict]]],
) -> None:
    """Пройти заданные слои находок: каждый закрыт, следующий принёс новые."""
    previous: tuple[int, int, list[dict], list[dict]] | None = None
    for generation, (confirmed, unresolved) in enumerate(layers, start=1):
        review_id = await _generation_with_findings(
            db, task_id, generation, confirmed=confirmed, unresolved=unresolved
        )
        if previous is not None:
            prev_review, prev_generation, prev_conf, prev_unres = previous
            await _author_closed_them(
                db,
                task_id,
                prev_review,
                prev_generation,
                confirmed=prev_conf,
                unresolved=prev_unres,
            )
        previous = (review_id, generation, confirmed, unresolved)


async def test_a_task_going_in_circles_is_named_and_escalated(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-1 (#1235): три захода — круг назван, и человека зовут решать.

    Образец взят с прода 09.09.2026 без округления. #1171: отчёт #307 дал
    три подтверждённые и пять неразрешённых, все восемь разобраны и
    закрыты, пересдача — и отчёт #323 принёс ПЯТЬ НОВЫХ неразрешённых. То
    же на #1208 и #1169. Ни один существующий потолок не сработал:
    review_cycle на #1171 оставался нулём при третьем заходе, потому что
    работу автору никто не возвращал — он пересдавал сам.

    Последний отчёт приходит НАСТОЯЩИМ путём приёма, а не записью в
    таблицу: круг обязан называться в тот момент, когда очередной отчёт
    принесли, иначе он не позовёт никого.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-1235"}, "run": {"id": "r-1235"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-circle")

    # Три первых слоя: каждый закрыт, следующий принёс новые находки.
    layer1 = ([_confirmed("гонка на записи", "concurrency", 10)], [_unresolved("A")])
    layer2 = ([_confirmed("необработанный код возврата", "error-handling", 20)], [])
    layer3 = ([_confirmed("тест не убивает мутацию", "test-adequacy", 30)], [])
    await _walk_the_circle(db, task_id, [layer1, layer2, layer3])

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, (
        "предпосылка: до четвёртого отчёта заходов два, круг ещё не назван"
    )
    assert await _circle_notices(db, task_id) == [], "порог ещё не достигнут"

    # Третий заход: закрываем слой 3 и принимаем ЧЕТВЁРТЫЙ отчёт как отчёт.
    await _author_closed_them(
        db,
        task_id,
        (await _last_review_id(db, task_id)),
        3,
        confirmed=layer3[0],
        unresolved=layer3[1],
    )
    await db.execute("UPDATE tasks SET submission_generation=4 WHERE id=?", (task_id,))
    await db.commit()
    from hub.services.machine_review_intake import record_machine_review
    from hub.models import MachineReviewSubmit

    await record_machine_review(
        db,
        task_id,
        MachineReviewSubmit(
            harness_skill="deep-review",
            model="grok-4.6",
            raw_count=1,
            incomplete=False,
            findings_confirmed=[_confirmed("утечка дескриптора", "resource-leak", 40)],
        ),
        principal_id=None,
        username="cursor-cloud-reviewer",
    )

    circle = await review_circle(db, task_id)
    assert circle.count == 3, "три захода: 1→2, 2→3, 3→4"

    notices = await _circle_notices(db, task_id)
    assert len(notices) == 1, (
        "круг обязан быть назван в карточке: пока он не назван, его не видит "
        "ни автор, ни человек, ни метрики"
    )
    said = notices[0]
    assert "3-й раз" in said, "число заходов названо"
    for ordinal in (1, 2, 3):
        assert f"заход {ordinal}" in said, "разбивка приводится по КАЖДОМУ заходу"
    assert "закрыто 2, пришло новых 1" in said, (
        "первый заход закрыл две находки (одну подтверждённую и одну "
        "неразрешённую) и получил одну новую — оба числа стоят рядом"
    )
    for outcome in ("принять как есть", "отпустить", "продолжать"):
        assert outcome in said, (
            "три исхода названы явно: сигнал о круге легко прочесть как "
            "разрешение перестать чинить настоящие дефекты"
        )
    assert "Ревью НЕ выключено и пересдача не запрещена" in said

    events = [
        dict(r)
        for r in await repo.list_events(
            db, since=0, kinds=["review_circle_named"], limit=10
        )
    ]
    assert len(events) == 1, "событие в ленте, а не только строка в карточке"
    assert json.loads(events[0]["payload"])["laps"] == 3

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "review", "сигнал не двигает задачу сам"
    assert row["review_cycle"] == 0, (
        "и это ровно тот случай, который существующий потолок не видит: "
        "циклов ревью ноль при третьем заходе (наблюдено на #1171)"
    )

    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.laps == 3 and brief.review_circle.named, (
        "число заходов видно и в брифе ревью, тем же счётом, что в карточке"
    )
    assert len(brief.review_circle.breakdown) == 3

    from hub.mcp_server import _review_circle_line

    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 3" in rendered and "заход 1" in rendered, (
        "ревьюер читает бриф ТЕКСТОМ: число, не дошедшее до строки, не прочитает никто"
    )


async def _last_review_id(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT id FROM machine_reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return int(dict(rows[0])["id"])


async def test_a_plain_resubmission_is_not_a_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2 (#1235): столько же пересдач, но находок между ними не было.

    МУТАЦИЯ, которую этот тест обязан ловить: считать заходом любую
    пересдачу. Счётчик, растущий на каждое поколение, назвал бы кругом
    обычную работу — задачу, которую пересдавали четыре раза с чистыми
    отчётами, — и позвал бы человека к тому, где решать нечего.

    Проверяется НОЛЬ заходов, а не отсутствие алерта: алерта нет и при
    пороге, которого не достигли, так что одно только молчание карточки
    отличить эти два случая не может.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-plain"}, "run": {"id": "r-plain"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-plain")
    for generation in (1, 2, 3, 4):
        await _generation_with_findings(db, task_id, generation)

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "четыре поколения с чистыми отчётами — это не круг, а работа: "
        "находок не было, закрывать было нечего"
    )
    assert not circle.named
    assert await name_the_circle(db, task_id) is False
    assert await _circle_notices(db, task_id) == []


async def test_a_closed_layer_without_new_findings_ends_the_circle(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Контроль к AC-2: находки были и закрыты, но нового слоя не пришло.

    Отдельный тест, потому что «находок не было вовсе» и «находки были,
    круг кончился» — разные состояния, и правило, потерявшее второе,
    прошло бы предыдущий тест целиком. Здесь же проверяется, что серия
    считается ПОДРЯД: чистый отчёт обнуляет счёт, а не откладывается в
    заслугу, по которой человека позовут к уже вышедшей задаче.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-end"}, "run": {"id": "r-end"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-ended")
    layer1 = ([_confirmed("гонка", "concurrency", 10)], [])
    layer2 = ([_confirmed("код возврата", "error-handling", 20)], [])
    layer3 = ([_confirmed("мутация выжила", "test-adequacy", 30)], [])
    await _walk_the_circle(db, task_id, [layer1, layer2, layer3])

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, "предпосылка: два захода"

    # Автор закрыл третий слой, и новый отчёт пришёл чистым.
    await _author_closed_them(
        db,
        task_id,
        await _last_review_id(db, task_id),
        3,
        confirmed=layer3[0],
        unresolved=layer3[1],
    )
    await _generation_with_findings(db, task_id, 4)

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "круг кончился на чистом отчёте — считается хвостовая серия подряд"
    )
    assert not circle.named


async def test_a_repeating_category_is_named_apart_from_the_count(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1235): повтор категории отмечен ОТДЕЛЬНО от числа заходов.

    Признак другой и важнее: три захода с находками разного рода — работа,
    идущая вглубь, а повтор категории означает, что харнесс ходит по одному
    и тому же месту. Поэтому проверяется не только наличие слов, но и то,
    что при РАЗНЫХ категориях отметки нет — иначе «отмечено отдельно» было
    бы совместимо с «отмечается всегда».
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-cat"}, "run": {"id": "r-cat"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-category")
    same = "test-adequacy"
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("мутация выжила в А", same, 10)], []),
            ([_confirmed("мутация выжила в Б", same, 20)], []),
            ([_confirmed("мутация выжила в В", same, 30)], []),
        ],
    )

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 2, "заходов два"
    assert circle.repeated_categories == (same,), (
        "категория повторяется — это отдельный факт, а не следствие счёта"
    )
    assert await name_the_circle(db, task_id) is True
    said = (await _circle_notices(db, task_id))[0]
    assert "ОТДЕЛЬНО" in said and same in said, "повтор назван своим абзацем"
    assert "харнесс ходит по одному" in said, (
        "названо, ЧТО этот признак означает, а не только что он есть"
    )

    # Контроль: те же два захода, но категории разные — отметки нет.
    other = await _submitted(client, db, "spike-1235-varied")
    await _walk_the_circle(
        db,
        other,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )
    varied = await review_circle(db, other)
    assert varied.count == 2, "заходов столько же"
    assert varied.repeated_categories == (), (
        "категории разные — повтора нет, и число заходов этого не подменяет"
    )
    assert await name_the_circle(db, other) is True
    assert "ОТДЕЛЬНО" not in (await _circle_notices(db, other))[0]


async def test_the_circle_threshold_comes_from_the_config(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог читается из конфига и проверен на трёх значениях.

    Он выбран по трём наблюдениям одного дня — для числа это недобор, и
    менять его придётся без правки кода. Значение, зашитое в коде, стоило
    бы выката на каждое уточнение.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-thr"}, "run": {"id": "r-thr"}})
    _wire(monkeypatch, recorder)

    task_id = await _submitted(client, db, "spike-1235-threshold")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import review_circle

    for threshold, expected in ((2, True), (3, False), (5, False)):
        monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", threshold)
        circle = await review_circle(db, task_id)
        assert circle.count == 2, "заходов два при любом пороге"
        assert circle.threshold == threshold
        assert circle.named is expected, (
            f"порог {threshold}: круг называется только когда заходов не меньше"
        )


async def test_the_circle_is_named_once_per_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Дедуп в пределах поколения: второй отчёт заходов не прибавляет.

    Лестница добора (#879) кладёт на одно поколение два отчёта. Без дедупа
    карточка получила бы вторую одинаковую запись про тот же круг.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-once"}, "run": {"id": "r-once"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-once")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import name_the_circle

    assert await name_the_circle(db, task_id) is True
    assert await name_the_circle(db, task_id) is False
    assert len(await _circle_notices(db, task_id)) == 1


async def test_findings_left_unclosed_are_not_a_lap(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Заход требует, чтобы находки БЫЛИ ЗАКРЫТЫ правкой.

    Пересдача, на которой автор объявил находки ложными или отложил их, —
    не круг: работы по коду на ней не было, а разговор про ложные находки
    — это разговор про точность харнесса, и у него свои метрики. Без этого
    теста мутация «считать заходом любую пересдачу, где отчёты были»
    осталась бы живой: предыдущий контроль ловит только пересдачу вообще
    без находок.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-open"}, "run": {"id": "r-open"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-unclosed")
    layers = [
        ([_confirmed("гонка", "concurrency", 10)], [_unresolved("A")]),
        ([_confirmed("код возврата", "error-handling", 20)], [_unresolved("B")]),
        ([_confirmed("утечка", "resource-leak", 30)], [_unresolved("C")]),
    ]
    previous: tuple[int, int, list[dict], list[dict]] | None = None
    for generation, (confirmed, unresolved) in enumerate(layers, start=1):
        review_id = await _generation_with_findings(
            db, task_id, generation, confirmed=confirmed, unresolved=unresolved
        )
        if previous is not None:
            prev_review, prev_generation, prev_conf, prev_unres = previous
            await _author_closed_them(
                db,
                task_id,
                prev_review,
                prev_generation,
                confirmed=prev_conf,
                unresolved=prev_unres,
                outcome_confirmed="false_positive",
                outcome_unresolved="not_a_defect",
            )
        previous = (review_id, generation, confirmed, unresolved)

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 0, (
        "находки были и новые приходили, но чинить автор ничего не стал — "
        "заходом это не считается"
    )
    assert await name_the_circle(db, task_id) is False
    assert await _circle_notices(db, task_id) == []


async def test_a_circle_of_uncategorised_findings_claims_no_repeat(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Круг из ОДНИХ неразрешённых находок не объявляет повтор категории.

    Это форма круга, который оплачивался живьём: #1171 — ноль
    подтверждённых и пять НОВЫХ неразрешённых, #1208 — четыре
    неразрешённые. У неразрешённой записи категории нет по модели (#1085),
    и здесь она остаётся пустой.

    МУТАЦИЯ, которую тест обязан ловить: снять отсев пустой категории в
    сравнении на повтор. Тогда «неизвестно» склеилось бы с «то же самое», и
    САМЫЙ СИЛЬНЫЙ сигнал этой задачи — «харнесс ходит по одному месту, и
    это важнее числа заходов» — срабатывал бы на каждом круге, собранном из
    неразрешённых находок, то есть на самом частом. Человека звали бы
    разбираться с повтором, которого никто не наблюдал, да ещё и с пустым
    именем категории в тексте.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-nocat"}, "run": {"id": "r-nocat"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-uncategorised")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([], [_unresolved("адъюдикаторы разошлись про А")]),
            ([], [_unresolved("адъюдикаторы разошлись про Б")]),
            ([], [_unresolved("адъюдикаторы разошлись про В")]),
        ],
    )

    from hub.services.review_dispatch import name_the_circle, review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 2, (
        "заходы считаются и по неразрешённым: круг с прода состоял в "
        "основном из них, и счёт по одним подтверждённым не увидел бы его"
    )
    assert circle.repeated_categories == (), (
        "категории у неразрешённых находок нет — это «неизвестно», а не «та же самая»"
    )
    assert [lap.repeated_categories for lap in circle.laps] == [(), ()]

    assert await name_the_circle(db, task_id) is True
    said = (await _circle_notices(db, task_id))[0]
    assert "ОТДЕЛЬНО" not in said and "Повтор категории" not in said, (
        "круг назван, а повтор категории — нет: это разные признаки"
    )


async def test_the_repeat_reaches_the_reviewer_brief_text(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Повтор категории доходит до СТРОКИ брифа, а не только до поля.

    Ревьюер читает бриф текстом (hub_get_review_brief склеивает его в
    строки), и признак, оставшийся в структуре, до него не доходит. Тест
    ловит мутацию, снимающую повтор из строки: число заходов в ней при
    этом остаётся, и по нему одному подмену не заметить.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-brief"}, "run": {"id": "r-brief"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-brief")
    same = "test-adequacy"
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("мутация выжила в А", same, 10)], []),
            ([_confirmed("мутация выжила в Б", same, 20)], []),
            ([_confirmed("мутация выжила в В", same, 30)], []),
        ],
    )

    from hub.mcp_server import _review_circle_line
    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.repeated_categories == [same]
    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 2" in rendered
    assert f"Повтор категории: {same}" in rendered, (
        "признак, не дошедший до строки брифа, ревьюер не прочитает"
    )
    assert "важнее числа заходов" in rendered


async def test_a_zero_threshold_switches_the_signal_off(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог 0 выключает сигнал, а не зовёт человека на каждую задачу.

    Ноль — это то, чем такую вещь выключают в конфиге, и без явной защиты
    сравнение «заходов не меньше порога» стало бы истинным при НУЛЕ
    заходов: карточка каждой задачи получила бы алерт о круге, которого
    нет. Порог выбран по трём наблюдениям одного дня, так что выключатель
    ему понадобится раньше, чем следующее уточнение.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 0)

    from hub.services.review_dispatch import name_the_circle, review_circle

    quiet = await _submitted(client, db, "spike-1235-off-quiet")
    circle = await review_circle(db, quiet)
    assert circle.count == 0 and circle.threshold == 0
    assert not circle.named, "ноль заходов при пороге 0 — это не круг"
    assert await name_the_circle(db, quiet) is False
    assert await _circle_notices(db, quiet) == []

    # И на задаче, которая по кругу действительно идёт, ноль тоже молчит.
    spinning = await _submitted(client, db, "spike-1235-off-spinning")
    await _walk_the_circle(
        db,
        spinning,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )
    spun = await review_circle(db, spinning)
    assert spun.count == 2, "заходы считаются по-прежнему"
    assert not spun.named, "но при пороге 0 человека не зовут"
    assert await name_the_circle(db, spinning) is False


async def test_a_deeper_lap_is_named_again_on_the_next_generation(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Следующий заход называется СНОВА: дедуп живёт в пределах поколения.

    МУТАЦИЯ, которую тест обязан ловить: убрать номер поколения из метки
    дедупа. Одна запись за всю жизнь задачи прошла бы тест на дедуп в
    пределах одной сдачи целиком, а на проде означала бы, что про третий,
    четвёртый и пятый заходы человеку не скажут ничего — как раз тогда,
    когда круг стал дороже всего.
    """
    recorder = _DispatchRecorder(
        {"agent": {"id": "bc-again"}, "run": {"id": "r-again"}}
    )
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-again")
    layers = [
        ([_confirmed("гонка", "concurrency", 10)], []),
        ([_confirmed("код возврата", "error-handling", 20)], []),
        ([_confirmed("утечка", "resource-leak", 30)], []),
    ]
    await _walk_the_circle(db, task_id, layers)

    from hub.services.review_dispatch import name_the_circle, review_circle

    assert await name_the_circle(db, task_id) is True
    assert len(await _circle_notices(db, task_id)) == 1

    # Автор закрыл третий слой, пересдал, и четвёртый отчёт принёс новое.
    await _author_closed_them(
        db,
        task_id,
        await _last_review_id(db, task_id),
        3,
        confirmed=layers[2][0],
        unresolved=layers[2][1],
    )
    await _generation_with_findings(
        db,
        task_id,
        4,
        confirmed=[_confirmed("дескриптор не закрыт", "resource-leak", 40)],
    )

    assert (await review_circle(db, task_id)).count == 3, "заходов стало три"
    assert await name_the_circle(db, task_id) is True, (
        "новый заход — новая сдача — новая запись: молчание про углубившийся "
        "круг было бы худшим из возможных исходов"
    )
    notices = await _circle_notices(db, task_id)
    assert len(notices) == 2
    assert "3-й раз" in notices[1], "и во второй раз названо новое число заходов"


async def test_two_reports_on_one_generation_do_not_double_count_a_finding(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Разбивка «пришло новых N» считает НАХОДКИ, а не строки отчётов.

    Лестница добора (#879) кладёт на одно поколение два отчёта, и второй
    часто повторяет находки первого. Находки поколения сливаются в один
    список, поэтому длина списка — не число находок: тот же дефект,
    названный дважды, дал бы двойку там, где находка одна.

    Число дорогое: человек по нему решает, стоит ли продолжать круг. «Слой
    из двух находок» вместо одной — ровно то завышение, из-за которого он
    решит иначе.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-dbl"}, "run": {"id": "r-dbl"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 2)

    task_id = await _submitted(client, db, "spike-1235-double")
    first = [_confirmed("гонка на записи", "concurrency", 10)]
    review_id = await _generation_with_findings(db, task_id, 1, confirmed=first)

    # Поколение 2: ДВА отчёта. Второй повторяет находку первого и приносит
    # свою. Строк в слитом списке четыре, РАЗНЫХ находок — три.
    leak = _confirmed("утечка дескриптора", "resource-leak", 40)
    hang = _confirmed("необработанный код возврата", "error-handling", 50)
    starve = _confirmed("тест не убивает мутацию", "test-adequacy", 60)
    await _generation_with_findings(db, task_id, 2, confirmed=[leak, hang])
    await _generation_with_findings(db, task_id, 2, confirmed=[leak, starve])
    await _author_closed_them(db, task_id, review_id, 1, confirmed=first, unresolved=[])

    from hub.services.review_dispatch import review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 1, (
        "заход один: два отчёта на поколении — всё ещё одно поколение"
    )
    assert circle.laps[0].arrived == 3, (
        "находок три, а строк в слитом списке четыре: считать надо "
        "уникальные finding_uid. Число не константа — оно обязано ЗАВИСЕТЬ "
        "от размера слоя, иначе человеку показывают не то, по чему он решает"
    )
    assert "пришло новых 3" in circle.breakdown()[0]


async def test_the_lap_count_stands_on_the_card_before_the_threshold(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Счёт заходов виден в КАРТОЧКЕ, а не только в алерте после порога.

    Постановка требует, чтобы число было видно в карточке и в брифе, и
    комментарий к REVIEW_CIRCLE_THRESHOLD обещает то же при пороге 0:
    «заходы считаются по-прежнему и видны в карточке и в брифе, но
    человека не зовут». Пока число доходит до карточки одним алертом на
    пороге, до порога карточка выглядит так, будто круга нет вовсе, —
    а молчание читается как «чисто» (#516, #549).
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-card"}, "run": {"id": "r-card"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-card")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    assert await _circle_notices(db, task_id) == [], "порог не достигнут: не зовём"

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    circle = card.get("review_circle") or {}
    assert circle.get("laps") == 2, (
        "два захода из трёх обязаны стоять в карточке ДО порога: иначе "
        "человек видит круг только тогда, когда круг уже стал дорогим"
    )
    assert circle.get("named") is False, "видно — не значит позвали"
    assert len(circle.get("breakdown") or []) == 2, (
        "разбивка «закрыто / пришло новых» едет вместе с числом"
    )


async def test_a_switched_off_threshold_still_counts_on_the_card(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Порог 0 гасит ЗОВ, а не счёт: ровно то, что обещает конфиг.

    Выключатель, прячущий заодно и число, отнял бы у человека
    единственный способ увидеть, что выключил он не то.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-off"}, "run": {"id": "r-off"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 0)

    task_id = await _submitted(client, db, "spike-1235-off")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    circle = card.get("review_circle") or {}
    assert circle.get("laps") == 2 and circle.get("named") is False
    assert await _circle_notices(db, task_id) == []


async def test_an_incomplete_empty_report_does_not_wipe_the_laps(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполный отчёт без находок — «неизвестно», а не «чисто».

    Пустой ПОЛНЫЙ отчёт круг кончает: харнесс дочитал и не нашёл ничего.
    Неполный отчёт говорит о себе сам, что дочитал не всё, и лестница
    добора (#879) существует ровно затем, чтобы добрать непрочитанное. Тот
    же файл уже исключает incomplete из «код прочитан» по этой самой
    причине, а докстринг review_circle обещает, что поколение без сведений
    цепь не рвёт: «неизвестно» не равно «чисто».

    Пока цепь рвалась, пустая строка оставалась в machine_reviews
    навсегда, и КАЖДАЯ следующая сдача снова упиралась в ту же пару и
    снова сбрасывала хвост: круг переставал быть видимым насовсем.

    Наблюдено на этой же задаче 10.09.2026: отчёт #366 по поколению 2
    пришёл с incomplete=true и нулём находок, после чего хаб сам вызвал
    глубокий добор.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-inc"}, "run": {"id": "r-inc"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-incomplete")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.services.review_dispatch import review_circle

    assert (await review_circle(db, task_id)).count == 2, "предпосылка: два захода"

    # Поколение 4: НЕПОЛНЫЙ отчёт, находок ноль.
    await db.execute("UPDATE tasks SET submission_generation=4 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=4,
        harness_skill="lite-diff-review",
        model="grok-4.6",
        raw_count=0,
        incomplete=True,
        findings_confirmed="[]",
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()

    assert (await review_circle(db, task_id)).count == 2, (
        "неполный отчёт не рвёт цепь: он сам говорит, что дочитал не всё, "
        "и обнулять по нему счёт значит объявить «чисто» там, где сказано "
        "«неизвестно»"
    )

    # КОНТРОЛЬ: полный пустой отчёт круг по-прежнему кончает.
    await db.execute("UPDATE tasks SET submission_generation=5 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=5,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=0,
        incomplete=False,
        findings_confirmed="[]",
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()

    assert (await review_circle(db, task_id)).count == 0, (
        "а вот ПОЛНЫЙ пустой отчёт круг кончает: харнесс дочитал и не нашёл "
        "ничего, и звать человека к вышедшей задаче незачем"
    )


async def test_the_brief_text_carries_the_count_before_the_circle_is_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Строка брифа несёт число и ДО того, как круг назван.

    Все прочие проверки _review_circle_line идут при named=true, поэтому
    мутация «молчать, пока не названо» осталась бы зелёной: ревьюер
    очередной сдачи читал бы отчёт как первый ровно в том случае, ради
    которого строка и заведена.
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-txt"}, "run": {"id": "r-txt"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-brieftext")
    await _walk_the_circle(
        db,
        task_id,
        [
            ([_confirmed("гонка", "concurrency", 10)], []),
            ([_confirmed("код возврата", "error-handling", 20)], []),
            ([_confirmed("утечка", "resource-leak", 30)], []),
        ],
    )

    from hub.mcp_server import _review_circle_line
    from hub.services.review_brief import build_review_brief

    brief = await build_review_brief(db, task_id)
    assert brief.review_circle.laps == 2 and not brief.review_circle.named
    rendered = _review_circle_line(brief.model_dump())
    assert "Круг ревью: заходов 2" in rendered, (
        "число обязано стоять в ТЕКСТЕ брифа и до порога: ревьюер, не "
        "знающий, что предыдущий слой был разобран, читает отчёт как первый"
    )
    assert "заход 1" in rendered and "заход 2" in rendered


async def test_an_incomplete_report_that_found_things_still_counts(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Неполнота отменяет ЧИСТОТУ отчёта, а не его находки.

    Пропускается только отчёт, не принёсший НИЧЕГО: там сведений ноль.
    Неполный отчёт, который что-то нашёл, — обычный слой находок, и
    выбросить его значило бы спрятать заход, который человек оплатил.

    Наблюдено на этой же задаче 10.09.2026: глубокий отчёт #369 пришёл с
    incomplete=true И с находками — то есть это не редкий угол, а обычный
    исход лестницы добора (#879).
    """
    recorder = _DispatchRecorder({"agent": {"id": "bc-inc2"}, "run": {"id": "r-inc2"}})
    _wire(monkeypatch, recorder)
    monkeypatch.setattr(config, "REVIEW_CIRCLE_THRESHOLD", 3)

    task_id = await _submitted(client, db, "spike-1235-incomplete-found")
    first = [_confirmed("гонка на записи", "concurrency", 10)]
    review_id = await _generation_with_findings(db, task_id, 1, confirmed=first)

    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        harness_skill="deep-review",
        model="grok-4.6",
        raw_count=1,
        incomplete=True,
        findings_confirmed=json.dumps(
            [_confirmed("утечка дескриптора", "resource-leak", 40)],
            ensure_ascii=False,
        ),
        unresolved="[]",
        submitted_by="cursor-cloud-reviewer",
    )
    await db.commit()
    await _author_closed_them(db, task_id, review_id, 1, confirmed=first, unresolved=[])

    from hub.services.review_dispatch import review_circle

    circle = await review_circle(db, task_id)
    assert circle.count == 1, (
        "заход есть: отчёт неполон, но находку он принёс, и заход этой "
        "находкой и состоялся"
    )
    assert circle.laps[0].arrived == 1


# --- #1252: вторая дверь ревью после НАБЛЮДЁННОГО отказа облака -------------
#
# Замерено 10.09.2026 на проде: HTTP 400, код usage_limit_exceeded, тело
# называет причину («Background Agent requires at least $2 remaining until
# your hard limit»). Наблюдалось на задачах #1250 в 09:46 UTC и #1172 в 10:03.
_LIMIT_REFUSAL = cursor_cloud.Refusal(
    status=400,
    code="usage_limit_exceeded",
    detail="Background Agent requires at least $2 remaining until your hard limit",
)


def _no_local_path(monkeypatch) -> None:
    """Локальный путь ВЫКЛЮЧЕН явно, а не по счастливому умолчанию."""
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "")


async def test_a_refused_cloud_call_still_gets_a_report_from_the_local_path(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-1 (#1252): облако отказало НА СОЗДАНИИ — отчёт всё равно есть.

    Форж github, то есть облако сюда дотягивается и пробуется ПЕРВЫМ; это и
    есть случай, у которого второй двери не было вовсе. Проверяется
    НАБЛЮДАЕМЫМ СОСТОЯНИЕМ карточки — строка в machine_reviews текущего
    поколения под принципалом ревьюера, — а не тем, что функцию позвали.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client, db, "spike-second-door", policy={"review": "dispatch"}
    )
    await wait_for_local_runs()
    await db.commit()

    assert len(recorder.calls) == 1, (
        "порядок не меняется: облако пробуется ПЕРВЫМ и ровно один раз "
        "(повтор на 400 покупает тот же отказ)"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, (
        "ровно это и не получалось 10.09.2026: сдача оставалась без отчёта "
        "навсегда, потому что второго способа не было"
    )
    assert reports[0]["principal_id"] == reviewer_pid, (
        "отчёт ложится под принципалом РЕВЬЮЕРА, иначе гейт его не засчитает"
    )
    assert not reports[0]["self_reviewed"], (
        "self_reviewed=1 означал бы оплаченный впустую прогон (#1128)"
    )
    dispatches = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]
    assert [d["channel"] for d in dispatches] == ["local"], (
        "облачной строки нет — агент не создался; локальная одна"
    )
    assert dispatches[0]["status"] == "done"


async def test_a_run_that_ended_without_a_report_opens_the_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-2 (#1252): ВТОРОЕ место применения — прогон кончился без отчёта.

    Отказ здесь асинхронный и лежит в другом пути кода: агент создался,
    деньги потрачены, прогон дошёл до ERROR, а machine_review так и не
    пришёл (замерено 10.09.2026 на агентах bc-6e7cc3e1 и bc-9b077f68).
    Закрыть только синхронный случай значит оставить эту половину сдач без
    отчёта ровно так же, как сегодня.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-6e7cc3e1"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    reviewer_pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client, db, "spike-dead-run", policy={"review": "dispatch"}
    )
    assert len(recorder.calls) == 1, "облако пробуется первым и агент СОЗДАЁТСЯ"
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert any("отчёт НЕ сдан" in u for u in updates), (
        "отказ облака остаётся наблюдённым фактом в карточке"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1, (
        "второй дверью открывается и этот путь: одного потребителя правила недостаточно"
    )
    assert reports[0]["principal_id"] == reviewer_pid
    assert not reports[0]["self_reviewed"]
    channels = [
        dict(r)["channel"]
        for r in await db.execute_fetchall(
            "SELECT channel FROM review_dispatches WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]
    assert channels == ["cloud", "local"], (
        "облачный прогон был оплачен и остаётся в истории; локальный — второй"
    )


async def test_an_unconfigured_local_path_changes_nothing_on_github(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-3 (#1252): настройки нет — поведение байт в байт прежнее.

    Тот же форж и тот же отказ облака, но локальный путь не настроен. Тогда
    в карточке ровно тот же единственный алерт об отказе облака, ни одной
    новой записи и ни одной попытки запуска. Настройка, которой нет, не
    имеет права менять сегодняшний путь — и это проверяется ПОПЫТКОЙ, а не
    чтением кода: подставка на месте запуска процесса обязана остаться
    нетронутой.
    """
    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    _no_local_path(monkeypatch)
    launched: list[str] = []

    async def _never(prompt):
        launched.append(prompt)
        raise AssertionError("локального прогона тут быть не должно")

    monkeypatch.setattr(local_reviewer, "run_review", _never)

    task_id = await _submitted(
        client, db, "spike-no-local", policy={"review": "dispatch"}
    )
    await db.commit()

    assert launched == [], "ненастроенный путь не запускает НИЧЕГО"
    alerts = [
        dict(r)["content"]
        for r in await db.execute_fetchall(
            "SELECT content FROM task_updates WHERE task_id=? AND kind='alert'",
            (task_id,),
        )
    ]
    about_review = [a for a in alerts if "ревью НЕ вызвано" in a]
    assert len(about_review) == 1, (
        "алерт об отказе облака остаётся ОДИН — второго объяснения одному "
        "состоянию не заводим (#1188)"
    )
    assert "провайдер отказал" in about_review[0], about_review[0]
    assert "HTTP 400, usage_limit_exceeded" in about_review[0], about_review[0]
    assert not await db.execute_fetchall(
        "SELECT 1 FROM review_dispatches WHERE task_id=?", (task_id,)
    ), "ни одной новой строки диспетчера"
    assert not await repo.machine_reviews_of_generation(db, task_id, 1)
    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    assert not any("ЛОКАЛЬНО" in u for u in updates), (
        "карточка не обещает того, чего не было"
    )


async def test_the_card_names_which_provider_gave_the_report_and_why(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """AC-4 (#1252): карточка называет, КТО дал отчёт и ПОЧЕМУ не первый.

    Названной причиной отказа облака, а не общими словами. Отчёт без
    указания автора читается как облачный и вводит человека в заблуждение
    ровно так же, как сегодня вводит молчание. И «форж облаку недоступен»
    здесь было бы ЛОЖЬЮ: форж github, облако до него дотягивается.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client, db, "spike-named-author", policy={"review": "dispatch"}
    )
    await wait_for_local_runs()
    await db.commit()

    updates = [dict(u)["content"] for u in await repo.get_task_updates(db, task_id)]
    started = [u for u in updates if "запущено ЛОКАЛЬНО" in u]
    assert len(started) == 1, "запуск второй двери называется в карточке"
    note = started[0]
    assert "ВТОРЫМ поставщиком" in note and "локальным" in note, (
        "кто дал отчёт — сказано поимённо"
    )
    assert "usage_limit_exceeded" in note and "HTTP 400" in note, (
        "почему не первый — НАЗВАННОЙ причиной отказа облака, с кодом "
        "провайдера, а не «облако недоступно»"
    )
    assert "недоступен" not in note, (
        "форж github облаку доступен; старый текст на этом месте был бы ложью"
    )


async def _cloud_dispatch_that_died(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path, slug: str
) -> int:
    """Задача с облачным заказом, чей прогон кончился ERROR без отчёта."""
    recorder = _DispatchRecorder({"agent": {"id": "bc-stale"}, "run": {"id": "r-s"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    task_id = await _submitted(client, db, slug, policy={"review": "dispatch"})
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    return task_id


async def _local_dispatches(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND channel='local'",
            (task_id,),
        )
    ]


async def test_the_second_door_does_not_buy_a_run_for_a_superseded_submission(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252: свип разбирает заказ ПОЗЖЕ, и сдача могла успеть смениться.

    Заказ был на поколение 1, автор с тех пор пересдал. Купить второго
    ревьюера по мёртвому поколению — это оплатить чтение кода, которого на
    ветке уже нет, и положить в карточку отчёт не про ту сдачу.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    task_id = await _cloud_dispatch_that_died(
        client, db, monkeypatch, tmp_path, "spike-superseded"
    )
    await db.execute(
        "UPDATE tasks SET submission_generation = 2 WHERE id = ?", (task_id,)
    )
    await db.commit()

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "вторая дверь открывается по ЖИВОЙ сдаче, а не по той, которую заказ "
        "успел пережить"
    )


async def test_the_second_door_stays_shut_on_a_task_that_left_review(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252: задача ушла из review — отчёт больше некому засчитывать.

    Свип бежит по расписанию, и между заказом и его разбором задачу могли
    вернуть в работу. Прогон по ней был бы куплен впустую: гейта, который
    ждёт этот отчёт, больше нет.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    task_id = await _cloud_dispatch_that_died(
        client, db, monkeypatch, tmp_path, "spike-left-review"
    )
    await db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    await db.commit()

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "ревьюер не покупается для задачи, которая ревью больше не ждёт"
    )


# --- #1252, второй заход ревью: находки 1–3 --------------------------------


async def test_a_blind_creation_does_not_open_the_second_door(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 1: СЛЕПОЙ исход — не отказ, и дверь на нём не открывается.

    Ответ на создание не дошёл, и спросить провайдера, создался ли агент,
    тоже не вышло. Это состояние ``_Started.blind``: агент, возможно, УЖЕ
    создан и оплачен. Локальный путь на нём купил бы ВТОРОГО ревьюера на ту
    же сдачу и принёс бы два соперничающих отчёта — ровно то, ради чего
    запрет на слепой повтор и стоит двумя строками выше по коду.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(
        None, refusal=cursor_cloud.Refusal(status=0, detail="таймаут ответа")
    )
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    async def _cannot_ask(name, pages=3):
        return cursor_cloud.Reconciliation("", "", False)

    monkeypatch.setattr(cursor_cloud, "find_agent_by_name", _cannot_ask)

    task_id = await _submitted(
        client, db, "spike-blind-door", policy={"review": "dispatch"}
    )
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "агент, возможно, УЖЕ создан — локальный путь купил бы второго судью"
    )


async def test_the_replacement_keeps_the_profile_the_top_up_ordered(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 2: замена упавшего добора обязана остаться deep.

    Принудительный добор лестницы (#879) заказал deep, прогон кончился без
    отчёта, и локальная замена считала профиль ЗАНОВО — на сдаче низкого
    риска это lite. Хуже понижения его последствие: упавший заказ и его
    замена вместе выводят счёт заходов за REVIEW_LADDER_MAX_STEPS, и
    неполный отчёт lite нового deep уже не позовёт. Лестница ломается молча
    и в сторону более дешёвого прогона. Тот же класс, что находка 7ed386a8.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-deep"}, "run": {"id": "r-deep"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client, db, "spike-deep-replacement", policy={"review": "dispatch"}
    )
    first = await _any_dispatch_row(db, task_id)
    assert first["profile"] == LITE, "исходный прогон дешёвый — иначе добора нет"
    await repo.set_review_dispatch_status(db, first["id"], "done")
    await db.commit()

    assert await maybe_dispatch_review(db, task_id, force_profile=DEEP)
    top_up = await _any_dispatch_row(db, task_id)
    assert top_up["profile"] == DEEP and top_up["id"] != first["id"]

    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    local = await _local_dispatches(db, task_id)
    assert len(local) == 1, "замена должна быть"
    assert local[0]["profile"] == DEEP, (
        "добор заказывали ТЕМ ЖЕ профилем: понижение до lite ломает лестницу молча"
    )


async def test_the_sync_second_door_rechecks_the_submission_is_still_live(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """#1252, находка 3: свежесть перечитывает и СИНХРОННЫЙ путь.

    Сторожа свежести стояли только в свипе, а мест применения два: пока
    летел запрос на создание облачного агента, автор успел пересдать.
    Синхронный путь передавал локальному диспетчеру устаревшие задачу,
    ветку и поколение — и покупал ревью по замещённой сдаче. Один
    потребитель правила ≠ все.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    monkeypatch.setattr(config, "CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", "reviewer-token")

    async def _no_usage(agent_id, run_id=None):
        return None

    monkeypatch.setattr(cursor_cloud, "get_usage", _no_usage)

    async def _refuse_and_resubmit(**kwargs):
        # Пока летел запрос, автор пересдал: поколение уже другое.
        await db.execute(
            "UPDATE tasks SET submission_generation = 2 "
            "WHERE submission_generation = 1 AND status = 'review'"
        )
        return None, _LIMIT_REFUSAL

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _refuse_and_resubmit)

    task_id = await _submitted(
        client, db, "spike-sync-stale", policy={"review": "dispatch"}
    )
    await wait_for_local_runs()
    await db.commit()

    assert await _local_dispatches(db, task_id) == [], (
        "сдача сменилась, пока летел запрос: локальный прогон купил бы чтение "
        "кода, которого на этой сдаче уже нет"
    )


# --- #1252, сдача №3: находки внешнего ревьюера по коммиту c939276 ----------


async def test_the_ceiling_counts_what_the_failed_run_already_cost(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 1 (P1): потолок расходов не видел денег УПАВШЕГО прогона.

    На асинхронном пути свип строкой выше спрашивает у провайдера счёт за
    прогон и кладёт его в ``review_dispatches.provider_tokens`` — сумма уже
    НАЗВАНА. А потолок считал ``_tokens_already_spent``, и та суммировала
    только ``machine_reviews``, где строки нет: отчёта-то не было. Оплаченный
    прогон учитывался как ноль ровно в тот момент, когда открывается вторая
    дверь, и ``LOCAL_REVIEW_TOKEN_CEILING`` (deploy/LOCAL-REVIEW.md)
    обходился.
    """
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-costly"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())
    monkeypatch.setattr(config, "LOCAL_REVIEW_TOKEN_CEILING", 1_000)

    task_id = await _submitted(
        client, db, "spike-ceiling-blind", policy={"review": "dispatch"}
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    async def _billed(agent_id, run_id=None):
        return {"totalUsage": {"totalTokens": 5_000}}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)
    monkeypatch.setattr(cursor_cloud, "get_usage", _billed)

    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    cloud = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM review_dispatches WHERE task_id=? AND channel='cloud'",
            (task_id,),
        )
    ]
    assert cloud and cloud[0]["provider_tokens"] == 5_000, (
        "счёт за упавший прогон СКАЗАН провайдером и записан — это не незнание"
    )
    assert await _local_dispatches(db, task_id) == [], (
        "потолок в 1000 токенов уже пробит оплаченным прогоном: вторая дверь "
        "не имеет права покупать ещё один"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("потолок стоимости исчерпан" in a for a in alerts), (
        "отказ по потолку называет причину, а не молчит (#1152)"
    )


async def test_a_crash_before_the_second_door_does_not_lose_it(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, tmp_path
):
    """Находка 2 (P2): вторая попытка терялась НАВСЕГДА.

    Строка облачного заказа переводилась в ``failed`` и коммитилась ДО вызова
    второй двери. Любой сбой после этого коммита — упавший процесс хаба или
    исключение в подготовке заказа (git-операции ходят в сеть) — оставлял
    сдачу без локального заказа, а свипы грузят только активные строки.
    Обещанной второй двери больше не существовало: поллер глотает исключение
    (hub/poller.py: «the sweep must not kill the loop») и идёт дальше.
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder({"agent": {"id": "bc-crash"}, "run": {"id": "r-1"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    task_id = await _submitted(
        client, db, "spike-crash-second-door", policy={"review": "dispatch"}
    )
    await db.execute(
        "UPDATE review_dispatches SET created_at = datetime('now', '-60 minutes')"
    )
    await db.commit()

    async def _errored(agent_id, run_id):
        return {"id": run_id, "status": "ERROR"}

    monkeypatch.setattr(cursor_cloud, "get_run", _errored)

    real_order = rd.prepare_review_order
    attempts = {"n": 0}

    async def _flaky_order(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("подготовка заказа сорвалась: git-операция не прошла")
        return await real_order(*args, **kwargs)

    monkeypatch.setattr(rd, "prepare_review_order", _flaky_order)

    try:
        await sweep_review_dispatches(db)
    except RuntimeError:
        pass  # ровно то, что поллер глотает и забывает
    await db.commit()

    # Перезапуск: следующий проход свипа обязан ЗАСТАТЬ долг второй двери.
    await sweep_review_dispatches(db)
    await wait_for_local_runs()
    await db.commit()

    assert len(await _local_dispatches(db, task_id)) == 1, (
        "долг второй двери переживает сбой: иначе сдача теряет обещанный "
        "второй способ добыть отчёт безвозвратно"
    )
    reports = [
        dict(r) for r in await repo.machine_reviews_of_generation(db, task_id, 1)
    ]
    assert len(reports) == 1
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert" and "отчёт НЕ сдан" in dict(r)["content"]
    ]
    assert len(alerts) == 1, (
        "повтор долга не превращается в поток алертов: отказ облака назван один раз"
    )


@pytest.mark.parametrize(
    "reads_before_revocation",
    # Чтений принципала на пути второй двери ТРИ, и отзыв токена между любыми
    # двумя из них даёт разное состояние. 1 — отозван после проверки
    # готовности у зовущего: ways вызываемого уже без локального канала.
    # 2 — отозван после ЕГО собственного review_reach: ways ещё с локальным
    # каналом, а принципала уже нет. Второй случай ловится только
    # перечитыванием принципала, и без него первый сторож пропускает.
    (1, 2),
    ids=("revoked-before-the-callee-looks", "revoked-between-reach-and-order"),
)
async def test_the_local_order_refuses_without_the_reviewers_principal(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    tmp_path,
    reads_before_revocation: int,
):
    """Находка 3 (P2): заказ мог уехать БЕЗ принципала ревьюера.

    ``open_second_door`` требует LOCAL_CHANNEL в ways, а вызываемый
    ``dispatch_local_review`` делал СВОЙ ``review_reach`` и смотрел только на
    ``runnable``. На github ``runnable`` истинно из-за облака даже тогда,
    когда локального канала уже нет, — и ``principal_id`` мог оказаться
    None. Между двумя чтениями лежит await, и токен ревьюера успевает быть
    отозванным: заказ создавался под NULL, то есть отчёт переставал быть
    чужим автору — ровно то, что обесценивает прогон (#1128).
    """
    from hub.services import review_dispatch as rd
    from hub.services.review_dispatch import wait_for_local_runs

    recorder = _DispatchRecorder(None, refusal=_LIMIT_REFUSAL)
    _wire(monkeypatch, recorder)
    pid = await _local_principal(db, monkeypatch)
    _stub_reviewer(monkeypatch, tmp_path, _reporting_stub())

    real_principal = rd.local_reviewer_principal_id
    reads = {"n": 0}

    async def _revoked_after_the_first_read(conn):
        reads["n"] += 1
        got = await real_principal(conn)
        if reads["n"] == reads_before_revocation:
            # Токен ревьюера отозвали ПОСЛЕ того, как готовность уже сказали.
            await conn.execute(
                "UPDATE api_keys SET revoked_at = datetime('now') "
                "WHERE principal_id = ?",
                (pid,),
            )
            await conn.commit()
        return got

    monkeypatch.setattr(
        rd, "local_reviewer_principal_id", _revoked_after_the_first_read
    )

    task_id = await _submitted(
        client, db, "spike-no-principal", policy={"review": "dispatch"}
    )
    await wait_for_local_runs()
    await db.commit()

    _rows = await _local_dispatches(db, task_id)
    assert [(r["id"], r["reviewer_principal_id"]) for r in _rows] == [], (
        "заказ без принципала независимого ревьюера не создаётся вовсе: "
        "отчёт под NULL читается как свой собственному вызову"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("LOCAL_REVIEWER_HUB_TOKEN" in a for a in alerts), (
        "отказ называет, ЧЕГО не хватает, именем настройки (#1083)"
    )


async def test_the_local_order_refuses_a_forge_where_only_the_cloud_is_ready(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Находка 3, вторая половина: правило живёт в ВЫЗЫВАЕМОЙ функции.

    Проверяется собственный вход ``dispatch_local_review``, а не путь через
    ``open_second_door``: сегодня сторож стоит у зовущего, и достаточность
    этого — свойство сегодняшнего списка зовущих, а не функции. На форже из
    CLOUD_REVIEW_FORGES ``reach.runnable`` истинно ИЗ-ЗА ОБЛАКА даже тогда,
    когда локального канала нет вовсе, и старая проверка пропускала заказ
    дальше: строка диспетчера создавалась под путь, которым исполнить её
    нечем.
    """
    from hub.services.review_dispatch import dispatch_local_review

    await _local_principal(db, monkeypatch)
    # Токен ревьюера есть и разрешается, а CLI и песочницы нет: локального
    # канала нет, но ways на github непуст — там стоит облако.
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", "")

    task_id = await _submitted(client, db, "spike-cloud-only-reach")
    task = dict(await repo.get_task(db, task_id))

    assert not await dispatch_local_review(db, task, "github", task["branch"], 1), (
        "локального канала нет — заказывать нечем"
    )
    assert await _local_dispatches(db, task_id) == [], (
        "строка диспетчера под путь, которым исполнить нечем, — оплаченный "
        "заказ в никуда"
    )
    alerts = [
        dict(r)["content"]
        for r in await repo.get_task_updates(db, task_id)
        if dict(r)["kind"] == "alert"
    ]
    assert any("LOCAL_REVIEW_CMD" in a for a in alerts), (
        "отказ называет отсутствующие настройки по именам (#1083)"
    )
