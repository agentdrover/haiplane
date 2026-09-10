"""Hub-dispatched cross-model reviews (#757).

The hub calls the reviewer, not the implementer; failures alert once and
change nothing; a run that finished without a report fails loudly; a
report whose tokens disagree with the provider's usage is flagged.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shlex
import stat
import subprocess
import textwrap
import uuid
from pathlib import Path

import aiosqlite
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
    _REVIEW_MODEL_PREFERENCES,
    _delivery_block,
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


def _fake_sudo(tmp_path) -> str:
    """Исполняемый скрипт по имени ``sudo`` — ради ФОРМЫ, а не ради изоляции.

    С 10.09.2026 хаб принимает закрытый набор форм строки песочницы (#1208), и
    системный ``env``, которым эти тесты пользовались раньше, в набор не
    входит: строка вне набора не запускается вовсе, и прогон, который тест
    хочет измерить, просто не состоялся бы. Второго unix-пользователя на
    машине разработчика не завести, поэтому здесь стоит скрипт, который
    ничего не изолирует, а лишь съедает свои три токена и запускает остальное.

    Смысл тот же, что был у ``env``: доказать, что префикс ДЕЙСТВИТЕЛЬНО
    применяется к командной строке — заглушка видит себя запущенной через
    него. Настоящая изоляция остаётся свойством выката
    (deploy/LOCAL-REVIEW.md), и её проверка попыткой названа в AC-2 ручной
    честно, а не подменена этим тестом.
    """
    path = tmp_path / "sudo"
    if not path.exists():
        path.write_text(
            '#!/bin/sh\n# sudo -n -u <пользователь> <команда...>\nshift 3\nexec "$@"\n'
        )
        path.chmod(0o755)
    return str(path)


def _sudo_sandbox(tmp_path, wrapper: str) -> str:
    """Строка песочницы формы ``sudo`` — той самой, что стоит на проде.

    Пользователь — сам вызывающий: чужого на машине разработчика нет, а страж
    проверяет его членство в группе каталога прогонов по-настоящему.
    """
    import os
    import pwd

    return f"{_fake_sudo(tmp_path)} -n -u {pwd.getpwuid(os.getuid()).pw_name} {wrapper}"


def _stub_reviewer(monkeypatch, tmp_path, script: str) -> None:
    """Локальный ревьюер = python-заглушка под настоящим префиксом."""
    import shlex
    import sys

    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", script]))
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
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    run = await local_reviewer.run_review("промт", timeout=1)

    assert run is not None and run.timed_out
    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "ревьюер пережил собственный таймаут: хаб написал в ленту, что снял "
        "процесс, и это было бы неправдой"
    )


async def test_the_run_guard_judges_the_deadline_the_run_will_get(
    monkeypatch, tmp_path
):
    """Стража спрашивают НА КАЖДОМ ПРОГОНЕ, а не только на готовности.

    Находка 10.09.2026 (ревьюер Codex, воспроизведено на ef2198fc): проверка
    сравнивала срок контейнера с ``LOCAL_REVIEW_TIMEOUT_SEC`` даже там, где
    прогону отмерили меньше. ``podman run --timeout 1800`` проходил готовность
    при умолчании 1800, а ``run_review(timeout=1)`` убивал только клиента
    podman — контейнер жил оставшиеся почти полчаса, жёг CPU и мог прислать
    отчёт по закрытому прогону.

    ПОВОРОТ 10.09.2026. Половина про СРОК потеряла предмет: контейнерных
    запусков в песочнице больше не бывает (закрытый набор форм, #1208), а обе
    формы набора оставляют полезную нагрузку потомком хаба — убийство группы
    доходит до неё при любом сроке, и суждения, зависящего от ``timeout``, у
    стража не осталось. Вход не выброшен: та же строка стоит здесь же и
    проверяется на отказ. Вторая половина осталась целиком и она несущая:
    отказ — это не мнение, а незапуск, иначе страж был бы суждением, которое
    некому применить.
    """
    sandbox = "/usr/bin/podman run --rm -i --timeout 1800 img"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", 1800)
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "cursor-agent --print")
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    assert local_reviewer.sandbox_problem(), (
        "контейнерный запуск в набор форм не входит — иначе проверка ниже беспредметна"
    )

    # И это не только суждение: прогон НЕ ЗАПУСКАЕТСЯ. Иначе страж остался бы
    # мнением, которое некому применить, — весь класс дефектов #1208 именно
    # об этом.
    spawned: list[str] = []
    monkeypatch.setattr(
        local_reviewer,
        "_spawn",
        lambda *a, **k: spawned.append("да"),  # noqa: ARG005
    )
    assert await local_reviewer.run_review("промт", timeout=1) is None, (
        "прогон с песочницей вне закрытого набора форм запускать нельзя"
    )
    assert not spawned, (
        "страж высказался, а хаб всё равно породил процесс: контейнер "
        f"пережил бы прогон на 1799 с, {spawned}"
    )


async def test_two_local_runs_never_overlap_on_the_host(monkeypatch, tmp_path):
    """Хост держит один прогон разом, и это держит ХАБ, а не скрипт враппера.

    Находка 10.09.2026 (ревьюер Codex, подтверждена на хосте хаба). Рабочий
    враппер снимал «хвосты» строкой ``podman rm -af``, называя основанием
    «два ревьюера разом хосту не по карману». Хаб такого ограничения не знал:
    ``_LOCAL_RUNS`` — обычный ``dict`` по идентификатору заказа, ни очереди,
    ни сериализации. Два ревью, начавшихся близко по времени, сносили друг
    друга, и в карточке это ложилось отказом прогона с ЛОЖНОЙ причиной.

    Доказательство внешнее: полезная нагрузка отмечает вход и выход в общем
    файле. Пересечение читается из ПОРЯДКА отметок, а не из времени — по
    времени тест был бы флаким на загруженной машине.
    """
    import shlex

    marks = tmp_path / "marks"
    payload = (
        f"printf 'in\n' >> {shlex.quote(str(marks))}; "
        "sleep 0.3; "
        f"printf 'out\n' >> {shlex.quote(str(marks))}"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", _scratch(tmp_path))
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")

    runs = await asyncio.gather(
        *[local_reviewer.run_review("промт", timeout=30) for _ in range(3)]
    )
    assert all(run is not None and not run.timed_out for run in runs), (
        f"прогоны не состоялись — тогда о пересечении судить не по чему: {runs}"
    )
    seen = marks.read_text().split()
    assert seen == ["in", "out"] * 3, (
        f"прогоны шли внахлёст: {seen}. На хосте это значит второй контейнер "
        "при отмеренных первому 1500 МБ и 1.5 CPU — и уборку враппера, "
        "которая сносит живой контейнер соседа"
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
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, "/bin/sh")
    )
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", shlex.join(["-c", payload]))
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
    import sys

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(["-c", "import sys; sys.stdin.read(); print('x' * 500000)"]),
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
    import sys

    probe = tmp_path / "mode.txt"
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
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
    import sys

    recorder = _DispatchRecorder({"agent": {"id": "bc-sd"}, "run": {"id": "r-sd"}})
    _wire(monkeypatch, recorder)
    await _local_principal(db, monkeypatch)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(["-c", "import time, sys; sys.stdin.read(); time.sleep(30)"]),
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

    import pwd

    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    # Песочница обязана быть ФОРМОЙ ИЗ НАБОРА, иначе not_ready() назовёт её, а
    # не каталог, и тест судил бы другое (#1208, поворот 10.09.2026: прежде
    # здесь стоял «/usr/bin/env», который набор не принимает). Пользователь —
    # сам вызывающий: он владеет каталогом, и по группе проходит.
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u {me} /usr/local/bin/wrap"
    )
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
        "/usr/bin/systemd-run --scope --uid=nobody /usr/local/bin/wrap",
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
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=1 /usr/local/bin/wrap",
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
        f"/usr/bin/systemd-run --scope --uid={os.getuid()} /usr/local/bin/wrap",
    )
    assert local_reviewer.not_ready() == [], "владелец каталога проходит по группе"

    # Пользователь, которого в системе нет: «не смогли проверить» — это тоже
    # причина, а не разрешение (неразрешённая 36a63d6b). Пустой not_ready()
    # означает «настроено», и вернуть его, ничего не проверив, значит
    # пообещать работу там, где ревьюер упрётся в EACCES.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=no-such-user-1180 /usr/local/bin/wrap",
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
    import sys
    import tracemalloc

    monkeypatch.setattr(local_reviewer, "OUTPUT_CAP", 1000)
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", _sudo_sandbox(tmp_path, sys.executable)
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_CMD",
        shlex.join(
            [
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


# --- #1208: документ выката и охват стражей песочницы -------------------------
#
# Найдено ВЫКАТОМ 08.09.2026, а не чтением: развёртывание локального ревьюера
# по собственному документу #1180 остановилось четырежды. Имена настроек в
# документе стояли без префикса HAIPLANE_, который подставляет config.env_get,
# — и это не ломало запуск, а МОЛЧА выключало локальный путь. Рекомендованная
# строка systemd-run --uid= от непривилегированного хаба не запускалась вовсе.
# А стражи знали ровно ту форму записи, которой на рабочей конфигурации нет.

_HUB_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_SOURCE = _HUB_ROOT / "hub/config.py"
_DEPLOY_DOC = _HUB_ROOT / "deploy/LOCAL-REVIEW.md"
_ENV_EXAMPLE = _HUB_ROOT / "deploy/local-hub.env.example"


def _documented_hub_deadline() -> int:
    """Хабский срок прогона, КАК ЕГО НАЗЫВАЕТ документ выката.

    Нужен там, где суждение стража зависит от ``LOCAL_REVIEW_TIMEOUT_SEC``:
    с 10.09.2026 контейнерный ``--timeout`` больше хабского отвергается, и
    тест, оставивший этот срок на волю окружения, был бы зелёным или красным
    по чужой переменной, а не по содержанию документа. Значение берётся ИЗ
    ФАЙЛА — переписанное сюда числом, оно проверяло бы тест, а не документ.
    """
    # Число берётся регулярным выражением, а не хвостом строки: то же имя со
    # значением стоит и внутри таблицы отказов, где за ним идёт разметка.
    named = re.compile(
        re.escape(config.brand.ENV_PREFIX + "LOCAL_REVIEW_TIMEOUT_SEC") + r"=(\d+)"
    )
    values = [
        found.group(1)
        for path in (_ENV_EXAMPLE, _DEPLOY_DOC)
        for line in path.read_text().splitlines()
        if (found := named.search(line))
    ]
    assert values, (
        f"в примере окружения и документе не назван {named.pattern} — "
        "проверка срока была бы привязана к переменной окружения, а не к "
        "документу"
    )
    assert len(set(values)) == 1, (
        f"документ называет хабский срок по-разному: {values}. Оператор "
        "скопирует одно из двух, и какое — неизвестно"
    )
    return int(values[0])


# Строка вида ``Environment=ИМЯ=…``, ``# ИМЯ=…`` или просто ``ИМЯ=…`` — то, что
# оператор КОПИРУЕТ к себе. Именно она и разошлась с кодом.
_ASSIGNED = re.compile(r"^\s*(?:#\s*)?(?:Environment=)?([A-Z][A-Z0-9_]*)=")
# Имя настройки локального ревьюера БЕЗ префикса: \b не срабатывает внутри
# HAIPLANE_LOCAL_REVIEW…, поэтому лишний lookbehind не нужен.
_BARE_NAME = re.compile(r"\bLOCAL_REVIEW[A-Z0-9_]*")


def _suffixes_read_by_config() -> set[str]:
    """Суффиксы env_get(...) из ИСХОДНИКА hub/config.py, а не из списка в тесте.

    Список имён, переписанный в тест, — третье описание тех же имён, и оно
    разойдётся следующим ровно так же, как разошёлся документ.
    """
    tree = ast.parse(_CONFIG_SOURCE.read_text())
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "env_get" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
    return {name for name in found if name.startswith("LOCAL_REVIEW")}


def _names_assigned_in(path: Path) -> set[str]:
    return {
        m.group(1)
        for line in path.read_text().splitlines()
        if (m := _ASSIGNED.match(line)) and "LOCAL_REVIEW" in m.group(1)
    }


def test_the_deploy_doc_names_the_settings_the_code_reads() -> None:
    """AC-1: имена в документе сверяются С ИСХОДНИКОМ машиной, а не глазами.

    Расхождение уже случилось один раз и было незаметным ровно потому, что
    глазами оно не ловится: HAIPLANE_HUB_HOST двумя строками выше в том же
    файле выглядит так же убедительно, как LOCAL_REVIEW_CMD без префикса.
    """
    read_by_code = _suffixes_read_by_config()
    assert read_by_code, (
        "разбор hub/config.py не нашёл ни одного env_get с именем "
        "LOCAL_REVIEW* — сверка была бы пустой и зелёной на любом документе"
    )
    expected = {config.brand.ENV_PREFIX + suffix for suffix in read_by_code}

    for path in (_DEPLOY_DOC, _ENV_EXAMPLE):
        named = _names_assigned_in(path)
        assert named == expected, (
            f"{path.name} называет настройки локального ревьюера как "
            f"{sorted(named)}, а код читает {sorted(expected)}. Разница в "
            "префиксе не ломает запуск, а МОЛЧА выключает локальный путь: в "
            "карточке встанет «локальный не настроен» при верной во всём "
            "остальном конфигурации (найдено выкатом 08.09.2026, #1208)"
        )

    # Ни одного упоминания без префикса — включая прозу: оператор, который
    # грепает по имени из текста, обязан найти то же самое имя.
    for path in (_DEPLOY_DOC, _ENV_EXAMPLE):
        bare = _BARE_NAME.findall(path.read_text())
        assert not bare, (
            f"{path.name} упоминает {sorted(set(bare))} без префикса "
            f"{config.brand.ENV_PREFIX} — а config.env_get читает только с ним"
        )


# ПЕРЕСМОТР 10.09.2026: закрытый набор форм вместо перечисления флагов.
#
# Тесты ниже НЕ выброшены и не ослаблены. Каждый из них кодирует настоящий
# вход, на котором страж когда-то ошибался, и все девять кругов ошибка шла в
# одну сторону — ложного ПРОПУСКА. Новое правило отвергает эти входы тем
# более, поэтому у большинства тестов входы остались прежними, а изменился
# ПРИГОВОР: там, где прежде проверялось «отвергнут по такому-то флагу»,
# теперь проверяется «отвергнут, и причина названа». Каждый такой поворот
# назван в докстроке своего теста поимённо — молча не перевёрнут ни один.


def test_every_blessed_sandbox_shape_still_passes(monkeypatch) -> None:
    """AC-6: рабочая строка прода и обе формы набора проходят стража.

    Строгое правило, отвергающее рабочий выкат, хуже прежнего мягкого,
    поэтому эта половина проверяется ТЕМ ЖЕ набором тестов, что и AC-5: иначе
    строгость чинилась бы ценой поломки, и поломка вскрылась бы на проде.

    Строки берутся из ``SANDBOX_SHAPES``, а не переписываются сюда: список,
    переписанный в тест, — третье описание тех же форм, и оно разойдётся
    следующим ровно так же, как пять раз расходился документ.
    """
    prod = "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", prod)
    assert local_reviewer.sandbox_problem() == [], (
        f"рабочая строка прода отвергнута: {local_reviewer.sandbox_problem()}"
    )
    assert local_reviewer.sandbox_uid() == "haiplane-reviewer"
    assert local_reviewer.sandbox_shape() == "sudo"

    for shape in local_reviewer.SANDBOX_SHAPES:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", shape.example)
        assert local_reviewer.sandbox_problem() == [], (
            f"форма «{shape.name}» отвергает собственный пример "
            f"«{shape.example}»: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_shape() == shape.name
        assert local_reviewer.sandbox_uid() == "haiplane-reviewer", (
            f"в примере формы «{shape.name}» страж не видит пользователя — "
            "значит проверка его членства в группе каталога прогонов на этой "
            "конфигурации молча не сработает вовсе, ровно как было до #1208"
        )

    # Необязательные флаги формы — тоже часть обещания: названный в документе
    # флаг обязан проходить, иначе документ рекомендует то, что хаб отвергнет.
    for sandbox in (
        "/usr/bin/systemd-run --scope --quiet --uid=haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=haiplane-reviewer --slice=review.slice "
        "--property=MemoryMax=1500M --property=CPUQuota=150% /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user=haiplane-reviewer /usr/local/bin/wrap",
        # Имя инструмента без пути: PATH прогону собирает сам хаб.
        "sudo -n -u haiplane-reviewer /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{sandbox}» состоит из флагов, названных в наборе, и отвергаться "
            f"не должна: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid() == "haiplane-reviewer"

    # Повтор НАКАПЛИВАЮЩЕГОСЯ флага — не тот повтор, что перекрывает: две
    # --property у systemd-run складываются, и запрещать их значило бы
    # отвергать строку с двумя лимитами, то есть рабочий рецепт.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=r --setenv=A=1 --setenv=B=2 /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == [], (
        f"--setenv накапливается, а не перекрывает: {local_reviewer.sandbox_problem()}"
    )


def test_a_sandbox_outside_the_closed_set_is_refused(monkeypatch) -> None:
    """AC-5: строка вне закрытого набора отвергается с названной причиной.

    Здесь собраны входы, каждый из которых КОГДА-ТО ПРОХОДИЛ стража, а
    обязан был быть отвергнут, — и входы, которые страж отвергал по частному
    флагу, а теперь отвергает по правилу. Ни один не выброшен: это
    накопленное за девять кругов доказательство, и оно переживает смену
    правила.

    Проверяется ДВА следствия разом. Первое: отказ есть и он назван. Второе:
    ``sandbox_uid()`` на такой строке пуст — раньше страж отвечал «кто» и про
    строку, которой не понял, и именно в этих ответах ошибался (имя из
    аргументов полезной нагрузки, из значения соседнего флага, из первого
    вхождения повторённого флага).
    """
    outside = [
        # Инструмент, о котором набор не знает вовсе. Прежнее ограничение
        # «чего не знаем — пропускаем» отменено решением владельца 10.09.2026.
        "/usr/bin/env",
        "/usr/bin/env -u HOME /usr/local/bin/wrap",
        "/bin/sh -c /usr/local/bin/wrap",
        # Относительное имя со слэшем: «какая-то программа», а не названная.
        "./sudo -n -u haiplane-reviewer /usr/local/bin/wrap",
        # Контейнерные движки — целиком, любой подкомандой и с любым сроком.
        "/usr/bin/podman run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout=1800 img",
        "/usr/bin/podman ps",
        "/usr/bin/docker run --rm -i img",
        "sudo -n -u haiplane-reviewer podman run --timeout 60 img",
        # Слипшийся короткий токен во всех записях, что находили читатели.
        "/usr/bin/sudo -nu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -nuhaiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -uhaiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -niu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -pu haiplane-reviewer /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u=haiplane-reviewer /usr/local/bin/wrap",
        # Повтор перекрывающего флага: инструмент возьмёт последнее вхождение,
        # читатель глазами — первое, и на этой разнице страж уже ошибался.
        "/usr/bin/sudo -n -u alice -u bob /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user alice --user bob /usr/local/bin/wrap",
        "/usr/bin/sudo -n --user=alice -u bob /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=alice --uid=bob /usr/local/bin/wrap",
        # Флаг, которого в форме нет.
        "/usr/bin/sudo -n -u haiplane-reviewer -g haiplane /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --pipe --uid=r /usr/local/bin/wrap",
        # Терминатор и аргументы полезной нагрузки в самой песочнице.
        "/usr/bin/systemd-run --scope --uid=alice -- /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u alice -- -u bob",
        "/usr/bin/systemd-run --scope --uid=alice /wrap --uid=bob",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap",
        # Пути к обёртке нет вовсе либо он относительный.
        "/usr/bin/sudo -n -u haiplane-reviewer",
        "/usr/bin/sudo -n -u haiplane-reviewer wrap",
        # Обязательного флага нет: без -n sudo может спросить пароль, а stdin
        # занят промтом с одноразовым кодом доступа к хабу.
        "/usr/bin/sudo -u 1234 /usr/local/bin/wrap",
        "/usr/bin/sudo /usr/local/bin/wrap",
        # Пользователь не назван вовсе — запуск шёл бы от самого хаба.
        "/usr/bin/sudo -n /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u  /usr/local/bin/wrap",
        # Флаг есть, значения нет: «флага нет» и «флаг пуст» — разные отказы,
        # и чинятся они по-разному.
        "/usr/bin/systemd-run --scope --uid= /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u",
        # И наоборот: значение написано флагу, который его не берёт.
        "/usr/bin/systemd-run --scope=1 --uid=r /usr/local/bin/wrap",
        # Одинокий дефис позиционным путём не является.
        "/usr/bin/sudo -n -u r -",
    ]
    for sandbox in outside:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"«{sandbox}» не совпадает ни с одной формой закрытого набора, а "
            "хаб пропустил её молча. Пустой список здесь означает «посмотрел "
            "и одобрил» — то есть ровно ту ошибку в сторону пропуска, из-за "
            "которой правило и переписано"
        )
        assert all(r.startswith("LOCAL_REVIEW_SANDBOX:") for r in reasons), (
            f"отказ обязан называть НАСТРОЙКУ, которую чинить: {reasons}"
        )
        assert all("deploy/LOCAL-REVIEW.md" in r for r in reasons), (
            f"отказ обязан указывать на документ, где набор форм назван: {reasons}"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"на отвергнутой строке «{sandbox}» страж назвал пользователя "
            f"«{local_reviewer.sandbox_uid()}». Прогона не будет, и судить о "
            "членстве в группе каталога прогонов не о чем — а имя, названное "
            "по непрочитанной строке, и есть источник всех девяти кругов"
        )
        assert local_reviewer.sandbox_shape() == ""

    # Пустая настройка второй причиной не шумит: её называет not_ready()
    # отдельной строкой, и повторять то же самое другими словами — это
    # два разных имени одной поломки в одной карточке.
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "")
    assert local_reviewer.sandbox_problem() == []


def test_the_guard_reads_the_sudo_form_of_the_sandbox_user(monkeypatch) -> None:
    """AC-2: форма sudo видна стражу так же, как --uid= от systemd-run.

    Рабочий рецепт выката использует ``sudo -n -u haiplane-reviewer``, а
    страж знал только ``--uid``. Он возвращал пустую строку, и проверка
    членства в группе каталога прогонов (заведённая ради находки ревью
    45971e09) на этой конфигурации не срабатывала ВОВСЕ — то есть создавала
    впечатление проверки там, где её нет.

    ПОВОРОТ 10.09.2026. Прежняя редакция этого теста держала таблицу «строка
    → имя пользователя» и на строках ВНЕ набора: ``sudo -nu X`` давало X,
    ``sudo -u alice -u bob`` — bob. Теперь такие строки не запускаются вовсе,
    и страж на них молчит; сами строки переехали в
    test_a_sandbox_outside_the_closed_set_is_refused, где проверяется их
    отказ. Здесь остались формы, которые набор ПРИНИМАЕТ, — и на них ответ
    обязан быть точным, потому что по нему судится доступ к каталогу.
    """
    cases = {
        # Три формы записи пользователя у sudo, названные в AC-2.
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n --user haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n --user=haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/sudo -n -u 1234 /usr/local/bin/wrap": "1234",
        # Старая форма НЕ сломана — иначе починено одно ценой другого.
        "/usr/bin/systemd-run --scope --uid=haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/systemd-run --scope --uid haiplane-reviewer /usr/local/bin/wrap": (
            "haiplane-reviewer"
        ),
        "/usr/bin/systemd-run --scope --uid=65534 /usr/local/bin/wrap": "65534",
    }
    for sandbox, expected in cases.items():
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{sandbox}» — форма из набора, и отвергаться она не должна: "
            f"{local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid() == expected, (
            f"песочница «{sandbox}» называет пользователя «{expected}», а "
            f"страж вернул «{local_reviewer.sandbox_uid()}»: пустая строка "
            "здесь означает, что проверка группы каталога прогонов молча не "
            "сработает вовсе"
        )


def test_the_user_guard_reads_only_the_wrappers_own_arguments(monkeypatch) -> None:
    """Имя пользователя не берётся из аргументов полезной нагрузки.

    Воспроизведено на HEAD 21629cde до починки — обе формы давали чужой
    ответ: ``sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap`` давало
    ``HOME`` (находка 64e89a8683b01db1), а ``systemd-run --scope --uid=alice
    -- /bin/true --uid=bob`` — ``bob`` (находка d228b0eb3310a9cc). Следствие
    названо: ``scratch_problem`` судил членство в группе каталога прогонов у
    пользователя, которого в строке нет вовсе.

    ПОВОРОТ 10.09.2026. Прежде тест требовал, чтобы на таких строках страж
    называл ПРАВИЛЬНОЕ имя. Теперь у песочницы аргументов полезной нагрузки
    не бывает вовсе: их дописывает сам хаб из LOCAL_REVIEW_CMD, а форма
    кончается путём к обёртке. Поэтому те же входы обязаны быть ОТВЕРГНУТЫ, а
    имя — не называться вообще: ответ по строке, которую хаб не запустит,
    никому не нужен, а именно такие ответы девять кругов и были неверны.
    """
    for sandbox in (
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME /wrap",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/bin/env -u HOME -u PATH /wrap",
        "/usr/bin/systemd-run --scope --uid=alice -- /bin/true --uid=bob",
        "/usr/bin/systemd-run --scope --uid alice -- /wrap --uid bob",
        "/usr/bin/systemd-run --scope --uid=alice /wrap --uid=bob",
        "/usr/bin/sudo -n -u alice -- -u bob",
        "/usr/bin/systemd-run --scope --uid=alice -- --uid=bob",
        "/usr/bin/sudo -n /wrap --uid=bob",
        "/usr/bin/sudo -n /usr/bin/env -u HOME /wrap",
        "/usr/bin/sudo -n -u alice -u bob /wrap --user carol",
        "/usr/bin/sudo -n /wrap podman run --timeout 60 --user 1000 img",
        "/usr/bin/sudo -nu haiplane-reviewer /usr/bin/env -u HOME /wrap",
        # --user за podman/docker — пользователь ВНУТРИ контейнера, а не на
        # хосте: принять его за хостового значило бы проверить членство в
        # группе каталога совсем не того пользователя.
        "/usr/bin/podman run --rm -i --timeout 60 --user 1000 img",
        "/usr/bin/docker run --user haiplane-reviewer img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» несёт аргументы полезной нагрузки прямо в песочнице "
            "— форма набора кончается путём к обёртке, и разбирать чужие "
            "флаги хаб не обязан и не берётся"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"в отвергнутой строке «{sandbox}» страж назвал "
            f"«{local_reviewer.sandbox_uid()}»: имя из аргументов полезной "
            "нагрузки означает, что членство в группе каталога прогонов "
            "проверяется НЕ У ТОГО пользователя"
        )


def test_the_scope_flag_is_read_only_from_systemd_runs_own_arguments(
    monkeypatch,
) -> None:
    """``--scope`` засчитывается только как СОБСТВЕННЫЙ флаг обёртки.

    Отказ уходил В СТОРОНУ ПРОПУСКА: страж искал ``--scope`` во всей строке,
    и ``--scope`` полезной нагрузки снимал отказ, хотя transient service от
    этого в scope не превращается. Воспроизведено на HEAD 21629cde: строка
    ``systemd-run --quiet --pipe --uid=x -- /wrap --scope`` возвращала ``[]``
    (находка 3d5938a9a645c39d).

    Отказ по НЕДОСТАЮЩЕМУ ОБЯЗАТЕЛЬНОМУ флагу идёт первым — раньше любого
    другого: оператору важнее узнать, что он забыл ``--scope``, чем что хаб
    не принимает ``--pipe``. Иначе он чинил бы по одному незнакомому флагу за
    круг, так и не увидев главного.
    """
    for hidden in (
        "/usr/bin/systemd-run --quiet --pipe --uid=x -- /wrap --scope",
        "/usr/bin/systemd-run --quiet --uid=x /wrap --scope",
        "/usr/bin/systemd-run --uid=x -- /usr/bin/env SCOPE=1 /wrap --scope",
        "/usr/bin/systemd-run --quiet --pipe --uid=haiplane-reviewer --",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", hidden)
        reasons = local_reviewer.sandbox_problem()
        # Подстроки «--scope» мало: она стоит и в ИМЕНИ формы, поэтому её
        # находит любой другой отказ этой же формы. Найдено мутацией
        # 10.09.2026: «if missing:» -> «if False:» оставляла тест зелёным,
        # потому что отказ по терминатору называет форму «systemd-run
        # --scope». Отказ обязан называть ПРИЧИНУ, а она одна — transient
        # service, переживающий снятие прогона.
        assert reasons and all(
            "--scope" in r and "transient service" in r for r in reasons
        ), (
            f"«{hidden}» — systemd-run БЕЗ --scope: ``--scope`` здесь стоит "
            "среди аргументов полезной нагрузки и transient service в scope "
            f"не превращает, а страж пропустил запуск молча: {reasons}"
        )

    # Собственный ``--scope`` обёртки по-прежнему снимает этот отказ — иначе
    # рабочая форма из #1180 оказалась бы сломана.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=x /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == [], (
        "«systemd-run --scope --uid=x /usr/local/bin/wrap» называет --scope "
        f"собственным флагом: {local_reviewer.sandbox_problem()}"
    )


def test_a_non_run_engine_call_does_not_hide_a_later_run(monkeypatch) -> None:
    """Движок отвергается любой подкомандой, а не только на ``run``.

    Отказ уходил В СТОРОНУ ПРОПУСКА: страж возвращал «движка нет» на первом
    же ``podman``, за которым не стоит ``run``, и настоящий запуск правее
    оставался невидимым. Воспроизведено на HEAD 21629cde:
    ``podman ps /usr/bin/podman run --timeout 0 img`` давало ``[]``, тогда
    как одиночный ``podman run --timeout 0`` отвергался (находка
    96144e6317c1d7ac).

    ПОВОРОТ 10.09.2026: искать подкоманду больше не нужно вовсе. Движок в
    закрытый набор форм не входит, и ``podman ps`` отвергается ровно так же,
    как ``podman run``, — вопрос «а не спрятан ли запуск правее» просто
    перестал существовать. Прежняя редакция теста ждала на ``podman ps``
    пустого списка, то есть ОДОБРЕНИЯ; это и есть поворот, и он назван.
    """
    for sandbox in (
        "/usr/bin/podman ps /usr/bin/podman run --timeout 0 img",
        "/usr/bin/podman version /usr/bin/podman run --rm -i img",
        "/usr/bin/docker ps /usr/bin/docker run --rm -i img",
        "/usr/bin/podman ps",
        "/usr/bin/podman ps /usr/bin/podman version",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» начинается с контейнерного движка, а он в набор "
            "форм не входит ни одной подкомандой: пропустить его значит "
            "разрешить прогон, который снять нельзя"
        )


def test_a_container_launch_without_its_own_deadline_is_refused(monkeypatch) -> None:
    """AC-3: контейнерный запуск отвергнут, и отказ называет НЕДОСТАЮЩИЙ ФЛАГ.

    Требование «прямой потомок хаба» для контейнеров недостижимо: измерено
    08.09.2026 — podman run переживает kill -KILL по группе, через 7 с
    контейнер «Up», потому что conmon отсоединён от клиента намеренно. Зато
    с --timeout 5 контейнер умирает сам (через 12 с живых нет).

    ПОВОРОТ 10.09.2026. Прежде отсюда следовало «потомок ИЛИ свой срок», и
    хаб разбирал строку podman до образа, чтобы этот срок найти. Разбор и дал
    шесть находок из пятнадцати, все — в сторону пропуска. Теперь
    контейнерный запуск отвергается ЦЕЛИКОМ, а срок жизни переехал внутрь
    root-ового враппера, где флаги зафиксированы и sudoers не допускает
    подстановок. Отказ по-прежнему обязан называть НЕДОСТАЮЩИЙ ФЛАГ, а не
    «песочница неверна»: оператору нужно знать, что во враппере обязан стоять
    ``--timeout``, и что у docker такого флага нет вовсе.

    Строки со сроком жизни (``--timeout 1800``), которые прежняя редакция
    теста требовала ПРИНИМАТЬ, теперь отвергаются вместе с остальными — и
    это тот самый поворот. Ни одна из них не выброшена: они стоят здесь же,
    ниже, и проверяются на отказ.
    """
    containers = [
        "/usr/bin/podman run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout=1800 img",
        "/usr/bin/podman run --rm -i -m 1500m --cpus 1.5 --pids-limit 512 "
        "--env-file /etc/haiplane-review/env --network none --timeout 1800 img",
        "/usr/bin/podman --log-level debug run --rm -i --timeout 1800 img",
        "/usr/bin/podman --url unix:///run/x run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -i img cursor-agent --timeout 60",
        "/usr/bin/podman run --rm -i img agent --timeout=60",
        "/usr/bin/podman --log-level debug run --rm -i img",
        "/usr/bin/podman -c remote run --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout=0 img",
        "/usr/bin/podman run --rm -i --timeout 00 img",
        "/usr/bin/podman run --rm -i --timeout abc img",
        "/usr/bin/podman run --rm -i --timeout",
        "/usr/bin/podman run --rm -i --timeout 1 img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout=1800 --timeout=0 img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout abc img",
        "/usr/bin/podman run --rm -i --timeout 1800 --timeout 5 --timeout 0 img",
        "/usr/bin/podman run --rm -i --timeout 0 --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout abc --timeout=1800 img",
    ]
    for sandbox in containers:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all("--timeout" in r for r in reasons), (
            f"«{sandbox}» — контейнерный запуск: отказ обязан назвать флаг, "
            f"без которого контейнер переживёт снятие прогона: {reasons}"
        )

    # У docker штатного аналога --timeout нет вовсе, и отказ обязан сказать,
    # ЧЕМ его заменить, а не просто «нельзя».
    for sandbox in (
        "/usr/bin/docker run --rm -i --timeout 60 img",
        "/usr/bin/docker run --rm -i img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and any("docker" in r and "podman" in r for r in reasons), (
            f"docker run обязан быть отвергнут с названной заменой: {reasons}"
        )

    # Старое требование не ослаблено: systemd-run без --scope — прежний отказ,
    # и назван по-прежнему недостающий флаг.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/systemd-run --quiet --pipe --uid=x --"
    )
    reasons = local_reviewer.sandbox_problem()
    assert reasons and all(
        "--scope" in r and "transient service" in r for r in reasons
    ), f"systemd-run без --scope остаётся отказом: {reasons}"
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=x /usr/local/bin/wrap",
    )
    assert local_reviewer.sandbox_problem() == []


def test_a_detached_container_launch_is_refused_by_flag(monkeypatch) -> None:
    """Отсоединённый запуск — отказ, и ``--timeout`` его не снимал никогда.

    Найдено ревьюером Codex 10.09.2026, воспроизведено на 5b7b813::

        podman run -d --timeout 1800 img        -> []       # пропуск
        podman run --detach --timeout=1800 img  -> []       # пропуск
        podman run --rm -i img                  -> ОТКАЗ    # правильно

    ``-d`` заставляет клиента podman вернуть управление НЕМЕДЛЕННО: хаб
    видит завершившийся процесс, закрывает прогон как законченный и может
    снести его каталог, а ревьюер внутри контейнера продолжает работать и
    способен прислать отчёт по уже закрытому прогону.

    ПОВОРОТ 10.09.2026. Прежде тест держал две половины: отсоединённые строки
    отвергнуть, «передний план» (``--detach=false``, ``-d=false``, ``-it``)
    принять — иначе починка была бы оплачена ложным отказом. Теперь
    отвергаются ОБЕ половины, потому что отвергается движок целиком, и
    ложного отказа тут нет: строка не «плоха», она вне набора, и починка
    названа — тот же podman внутри враппера. Важное осталось: отказ НЕ
    ВЫДУМЫВАЕТ отсоединения там, где его нет. ``--detach=false`` — это
    передний план, и обвинять строку в ``-d`` было бы ложью о ней.
    """
    detached = [
        "/usr/bin/podman run -d --timeout 1800 img",
        "/usr/bin/podman run --detach --timeout=1800 img",
        "/usr/bin/podman run --rm -i -d --timeout 1800 img",
        "/usr/bin/podman run -itd --timeout 1800 img",
        "/usr/bin/podman run -dt --read-only --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=true --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=false -d --timeout 1800 img",
    ]
    foreground = [
        "/usr/bin/podman run --rm -i --timeout 1800 img",
        "/usr/bin/podman run --rm -it --timeout 1800 img",
        "/usr/bin/podman run --rm -i --detach=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i -d=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i -d --detach=false --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout 1800 img agent -d",
    ]
    for sandbox in detached + foreground:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"«{sandbox}» — контейнерный запуск, и в набор форм он не входит"
        )

    for sandbox in foreground:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert not any("отсоедин" in r and "«-d»" in r for r in reasons), (
            f"«{sandbox}» идёт на переднем плане, и обвинять её в -d значит "
            f"назвать оператору причину, которой в строке нет: {reasons}"
        )


def test_a_container_deadline_may_not_outlive_the_hubs(monkeypatch) -> None:
    """Срок контейнера в песочнице не судится вовсе — контейнера там нет.

    Найдено ревьюером Codex 10.09.2026, воспроизведено на 5b7b813:
    ``podman run --rm -i --timeout 3600 img`` при хабских 1800 проходило
    молча, хотя контейнер переживает снятие прогона ровно так же, как без
    ``--timeout`` вовсе, — только тише.

    ПОВОРОТ 10.09.2026. Правило «не больше хабского» держалось на разборе
    чужой командной строки и на сравнении с числом, по которому прогон
    снимут. Обе опоры ушли вместе с контейнерами: приговор этому классу
    строк теперь ОДИН и от ``LOCAL_REVIEW_TIMEOUT_SEC`` не зависит вовсе.
    Это и проверяется — иначе поворот был бы заявлен, а не измерен. Сам срок
    жизни контейнера никуда не делся: он стоит во враппере, равен хабскому и
    держится тестами документа (test_the_doc_wrapper_deadline_follows_the_conf_file).
    """
    verdicts = set()
    for deadline in (1800, 7200):
        monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", deadline)
        for sandbox in (
            "/usr/bin/podman run --rm -i --timeout 3600 img",
            "/usr/bin/podman run --rm -i --timeout=1801 img",
            "/usr/bin/podman run --rm -i --timeout 1800 --timeout 7200 img",
            "/usr/bin/podman run --rm -i --timeout 1800 img",
            "/usr/bin/podman run --rm -i --timeout 60 img",
            "/usr/bin/podman run --rm -i --timeout 7200 --timeout 900 img",
            "/usr/bin/podman run --rm -i --timeout 7201 img",
        ):
            monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
            reasons = local_reviewer.sandbox_problem()
            assert reasons, f"«{sandbox}» — контейнерный запуск, и он отвергнут"
            verdicts.add((sandbox, tuple(reasons)))

    assert len({sandbox for sandbox, _ in verdicts}) == len(verdicts), (
        "приговор одной и той же строке разошёлся на разном хабском сроке — "
        "значит суждение всё ещё зависит от LOCAL_REVIEW_TIMEOUT_SEC, а "
        "зависеть ему больше не от чего"
    )


def test_the_run_guard_refuses_the_string_it_cannot_parse(monkeypatch) -> None:
    """Строка, которую страж не может прочитать целиком, — отказ, а не «ок».

    Воспроизведено на ff617db (найдено ревьюером Codex 10.09.2026)::

        podman run --timeout 60 --blkio-weight 500 --timeout 0 img
        detaching_sandbox() -> []            # пропуск

    ``--blkio-weight`` не был назван в списке флагов со значением, поэтому
    ``500`` принято за имя образа, разбор кончился, и ВТОРОЙ ``--timeout 0``
    остался невиден. У podman действующим будет последний, то есть ноль, то
    есть срока жизни нет.

    ПОВОРОТ 10.09.2026: списка флагов podman у хаба больше нет, и угадывать
    нечего — движок не входит в набор форм. Правило, ради которого тест
    написан, стало ШИРЕ: страж отвергает всё, что не прочитал целиком, а не
    только то, о чём успел вынести суждение. Поэтому здесь же проверяются
    строки, которые прежняя редакция ПРИНИМАЛА как «однозначные»
    (``--blkio-weight=500``, ``-itq``): они тоже вне набора.
    """
    for sandbox in (
        "/usr/bin/podman run --timeout 60 --blkio-weight 500 --timeout 0 img",
        "/usr/bin/podman run --blkio-weight 500 --timeout 1800 img",
        "/usr/bin/podman run --rm -i --blkio-weight 500 img",
        "/usr/bin/podman run --rm -i --blkio-weight=500 --timeout=1800 img",
        "/usr/bin/podman run --rm -it --privileged --timeout 1800 img",
        "/usr/bin/podman run -itq --read-only --timeout 1800 img",
        "/usr/bin/podman run --rm -i --timeout 1800 img cursor-agent --blkio-weight 5",
        # Тот же класс в форме, которую набор ЗНАЕТ по инструменту: флаг, о
        # котором форма не договаривалась, отвергается по имени.
        "/usr/bin/systemd-run --scope --uid=r --blkio-weight=500 /usr/local/bin/wrap",
        "/usr/bin/sudo -n -u r --blkio-weight=500 /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"«{sandbox}» страж прочитать целиком не может. Пустой список "
            "здесь означает «посмотрел и одобрил» — то есть контейнер без "
            "срока жизни пройдёт молча"
        )

    # Флаг, которого форма не знает, назван в отказе ПО ИМЕНИ: иначе
    # оператору не видно, что убирать.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/systemd-run --scope --uid=r --blkio-weight=500 /usr/local/bin/wrap",
    )
    assert any("--blkio-weight" in r for r in local_reviewer.sandbox_problem()), (
        "отказ обязан НАЗВАТЬ флаг, которого форма не знает: "
        f"{local_reviewer.sandbox_problem()}"
    )


def test_the_unknown_flag_refusal_advises_a_form_podman_has(monkeypatch) -> None:
    """Отказ советует починку, КОТОРАЯ РАБОТАЕТ, — и это проверяется запуском.

    Один шаблон на обе формы флага советовал невозможное: на коротком токене
    он предлагал ``-p80:80=<значение>`` — записи, которой у podman нет вовсе
    (pflag разберёт ``-p80:80=x`` как значение ``80:80=x`` у ``-p``), и
    оператор, сделавший ровно то, что сказано, получал тот же отказ.

    ПОВОРОТ 10.09.2026: советовать форму записи флага podman хаб больше не
    берётся — он не разбирает podman вовсе. Обещание осталось прежним и стало
    проверяемым строже: КАЖДЫЙ отказ называет починку, и починка приводит к
    строке, которую хаб принимает. Здесь это проверяется исполнением, а не
    чтением: сделали, как сказано, — прогнали через стража.
    """
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/podman run --rm -p80:80 --timeout 1800 img",
    )
    reasons = local_reviewer.sandbox_problem()
    assert reasons, "неизвестный короткий флаг обязан давать отказ"
    assert not any("-p80:80=<значение>" in r for r in reasons), (
        "отказ советует дописать «=<значение>» к целому короткому токену — "
        f"записи, которой у podman нет: {reasons}"
    )
    assert all("sudo -n -u" in r for r in reasons), (
        f"отказ обязан назвать форму, к которой оператору идти: {reasons}"
    )

    # Починка, названная в отказе, ИСПОЛНЯЕТСЯ и приводит к принятой строке.
    fixed = "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", fixed)
    assert local_reviewer.sandbox_problem() == [], (
        f"страж советует форму, которую сам же отвергает: "
        f"{local_reviewer.sandbox_problem()}"
    )

    # То же обещание на слипшемся токене: сказано «напишите отдельными
    # токенами» — делаем так и получаем принятую строку.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -nu r /usr/local/bin/wrap"
    )
    bundled = local_reviewer.sandbox_problem()
    assert bundled and all("отдельным токеном" in r for r in bundled), (
        f"отказ на слипшемся токене обязан назвать починку: {bundled}"
    )
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -n -u r /usr/local/bin/wrap"
    )
    assert local_reviewer.sandbox_problem() == [], (
        f"починка, названная в отказе, не работает: {local_reviewer.sandbox_problem()}"
    )


def test_a_consumed_value_does_not_pass_for_the_scope_flag(monkeypatch) -> None:
    """``--scope``, съеденный как ЗНАЧЕНИЕ соседа, флагом ``--scope`` не является.

    Неразрешённая 5a59af8d620685d0 машинного ревью #371. Воспроизведено на
    ff617db и на HEAD 149cd825::

        systemd-run --description --scope --uid=haiplane-reviewer /wrap
        окно -> ['--description', '--scope', '--uid=haiplane-reviewer']
        detaching_sandbox() -> []            # пропуск

    Настоящий systemd-run возьмёт ``--scope`` описанием и поднимет transient
    SERVICE, то есть ровно то, ради чего отказ и написан. Ошибка шла В
    СТОРОНУ ПРОПУСКА.

    Возражение опровергателя («рекомендованный рецепт — sudo-враппер, а не
    systemd-run») отклонено и здесь: страж существует не для рекомендованной
    строки, а для той, которую напишет оператор, — рекомендованную проверять
    было бы незачем. И systemd-run --scope остаётся формой набора.
    """
    for sandbox in (
        "/usr/bin/systemd-run --description --scope --uid=r /wrap",
        "/usr/bin/systemd-run --unit --scope --uid=r /wrap",
        "/usr/bin/systemd-run --slice --scope -- /wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all(
            "--scope" in r and "transient service" in r for r in reasons
        ), (
            f"в «{sandbox}» слово «--scope» стоит ЗНАЧЕНИЕМ соседнего флага, "
            "а не флагом. systemd-run поднимет transient service, который "
            f"переживёт снятие прогона: {reasons}"
        )

    # А рабочие формы, где --scope настоящий, приниматься не перестали:
    # починка не оплачена ложным отказом (возражение опровергателя учтено).
    for fine in (
        "/usr/bin/systemd-run --description=review --scope --uid=r /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --description review --uid=r /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", fine)
        assert local_reviewer.sandbox_problem() == [], (
            f"«{fine}» называет --scope собственным флагом systemd-run: "
            f"{local_reviewer.sandbox_problem()}"
        )

    # Та же поправка на пользователе: имя берётся у флага, а не у соседа.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo --prompt -u alice /wrap"
    )
    assert local_reviewer.sandbox_uid() == "", (
        "«-u» здесь съедено значением --prompt, и пользователя строка не "
        f"называет вовсе; страж назвал «{local_reviewer.sandbox_uid()}»"
    )


def test_the_run_options_end_at_the_double_dash(monkeypatch) -> None:
    """``--`` в песочнице не принимается — ни у движка, ни у формы набора.

    Неразрешённая a58d77e268b2d709 машинного ревью #371. Воспроизведено на
    ff617db и на HEAD 149cd825::

        podman run -- --timeout 1800 img
        окно флагов -> [('--', None), ('--timeout', '1800')]
        detaching_sandbox() -> []            # пропуск

    Для podman образ здесь — ``--timeout``, а ``1800`` и ``img`` уже команда
    внутри контейнера: собственного срока жизни у запуска НЕТ. Страж же
    находил ``--timeout 1800`` и засчитывал срок, ошибаясь В СТОРОНУ
    ПРОПУСКА. Хуже того, прошлый круг числил ``--`` среди беззначных флагов,
    то есть отказ по неизвестному флагу на этой строке заведомо не срабатывал.

    ПОВОРОТ 10.09.2026: терминатор не принимается ни в одной форме набора —
    за флагами стоит ровно один аргумент, путь к обёртке, и отделять от него
    нечего. Класс дефектов «что считается за ``--``» закрыт целиком, а не
    разобран правильнее.
    """
    for sandbox in (
        "/usr/bin/podman run -- --timeout 1800 img",
        "/usr/bin/podman run --rm -i -- --timeout 1800 img",
        "/usr/bin/podman run -- --rm -i img",
        "/usr/bin/podman run --rm -i --timeout 1800 -- img",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem(), (
            f"в «{sandbox}» всё, что стоит за «--», — это образ и команда "
            "внутри контейнера. Своего срока жизни у запуска нет, и "
            "контейнер переживёт снятие прогона"
        )

    for sandbox in (
        "/usr/bin/sudo -n -u haiplane-reviewer -- /usr/local/bin/wrap",
        "/usr/bin/systemd-run --scope --uid=r -- /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons and all("--»" in r for r in reasons), (
            f"«{sandbox}» пишет терминатор там, где отделять нечего, и отказ "
            f"обязан назвать именно его: {reasons}"
        )


def test_sudo_names_a_numeric_uid_with_a_hash(monkeypatch, tmp_path) -> None:
    """``sudo -u '#1000'`` — числовой uid, а не имя, которого нет в системе.

    sudo(8): «The user may be either a user name or a numeric user ID (UID)
    prefixed with the '#' character». Воспроизведено на ff617db (найдено
    ревьюером Codex 10.09.2026)::

        sudo -n -u #1000 /usr/local/bin/wrap
        sandbox_uid() -> '#1000'   _resolve_user('#1000') -> None

    ``'#1000'.isdigit()`` ложно, разбор уходил в ``getpwnam('#1000')`` и падал
    ВСЕГДА. Отказ шёл в безопасную сторону, но причину называл неверную:
    оператор читал «пользователь не разрешается в системе» про пользователя,
    который в системе есть.

    Решётка снимает неоднозначность именно У SUDO: там ``1000`` — это ИМЯ, а
    ``#1000`` — uid, и обе записи законны. У systemd-run такой формы нет, и
    принимать её там значит разрешить строку, которую сам systemd-run
    отвергнет, — поэтому тест держит и это.
    """
    import pwd

    me = pwd.getpwuid(os.getuid())
    uid = me.pw_uid

    # Слипшаяся запись той же решётки в набор форм не входит с 10.09.2026 —
    # но вход не выброшен: он проверяется на ОТКАЗ, а не на разбор.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -nu#{uid} /usr/local/bin/wrap"
    )
    assert local_reviewer.sandbox_problem(), (
        "слипшийся токен «-nu#N» набор форм не принимает: из одного токена не "
        "видно, где кончается флаг и начинается значение"
    )

    # Форма sudo с решёткой разрешается в НАСТОЯЩЕГО пользователя.
    for sandbox in (
        f"/usr/bin/sudo -n -u #{uid} /usr/local/bin/wrap",
        f"/usr/bin/sudo -n --user=#{uid} /usr/local/bin/wrap",
    ):
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        tool, user = local_reviewer._named_sandbox_user()
        entry = local_reviewer._resolve_user(user, tool)
        assert entry is not None and entry.pw_uid == uid, (
            f"«{sandbox}» называет uid {uid} синтаксисом самого sudo, а страж "
            f"его не разрешил ({tool!r}, {user!r} -> {entry}): годная "
            "настройка отвергается, и человеку называется ложная причина"
        )

    # Различение сохранено: у systemd-run решётки нет, и придумывать её там
    # нельзя — а голое число он принимает как uid по-прежнему.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid=#{uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert local_reviewer._resolve_user(user, tool) is None, (
        "у systemd-run записи --uid=#N не существует; принять её значило бы "
        "разрешить строку, на которой запуск упадёт уже внутри systemd-run"
    )
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    entry = local_reviewer._resolve_user(user, tool)
    assert entry is not None and entry.pw_uid == uid, (
        "числовая форма systemd-run (неразрешённая 534e16e4) не сломана"
    )

    # И обратная сторона того же правила: у sudo ГОЛОЕ число — это ИМЯ.
    # Замерено 10.09.2026 на машине разработки, где uid 501 существует и
    # принадлежит вызывающему::
    #
    #     sudo -n -u 501 true    -> «sudo: unknown user 501», rc 1
    #     sudo -n -u '#501' true -> rc 0
    #
    # Значит ``getpwuid`` на такой записи отвечает про пользователя, которого
    # sudo в строке НЕ ВИДИТ, — и страж одобрял бы песочницу, падающую на
    # первом запуске. Ревьюер называл только форму с решёткой; эта половина
    # доделана преемником.
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u {uid} /usr/local/bin/wrap"
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert (tool, user) == ("sudo", str(uid)), (
        f"пользователь из «sudo -u {uid}» не извлёкся ({tool!r}, {user!r}) — "
        "всё, что ниже, судило бы не тот вход"
    )
    bare = local_reviewer._resolve_user(user, tool)
    try:
        by_name = pwd.getpwnam(str(uid))
    except KeyError:
        by_name = None
    # РАВЕНСТВО, а не «None или имя из цифр». Прежнее утверждение
    # «bare is None or bare.pw_name == str(uid)» getpwnam от getpwuid не
    # отличало: на машине, где учётка названа цифрами собственного uid
    # (обычная запись в образах и в LDAP), обе ветки дают одну и ту же
    # запись, pw_name == str(uid), и регресс в getpwuid проходил бы
    # зелёным (неразрешённая ревью #373; проверено на struct_passwd
    # ('1000', uid 1000) — утверждение True на обеих ветках).
    assert bare == by_name, (
        f"«sudo -u {uid}» — это ИМЯ «{uid}», и страж обязан ответить ровно "
        f"то же, что getpwnam('{uid}') ({by_name}); он ответил {bare}. "
        "Разрешать голое число через getpwuid значит проверить членство в "
        "группе у постороннего и объявить настроенной песочницу, на которой "
        "sudo скажет «unknown user»"
    )

    # Та же строка, доведённая до САМОГО стража каталога прогонов: прежде эта
    # половина не проверялась вовсе, и «страж одобрял бы песочницу, падающую
    # на первом запуске» держалось одним лишь _resolve_user.
    scratch = tmp_path / "runs"
    scratch.mkdir()
    # chown ДО chmod: непривилегированный chown снимает setgid, и каталог
    # увёл бы scratch_problem() в ветку «нет setgid», где про пользователя не
    # спрашивают вовсе.
    os.chown(scratch, -1, me.pw_gid)
    scratch.chmod(0o2770)
    assert os.stat(scratch).st_mode & stat.S_ISGID, "каталог без setgid судит другое"
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    problems = local_reviewer.scratch_problem()
    if by_name is None:
        assert problems and all("не разрешается в системе" in p for p in problems), (
            f"«sudo -u {uid}» называет ИМЯ «{uid}», которого в системе нет. "
            "Страж обязан назвать это причиной, а не промолчать: молчание "
            f"здесь — обещание работы, которой не будет ({problems})"
        )
        assert any(f"«{uid}»" in p for p in problems), (
            f"отказ обязан назвать неразрешённого пользователя: {problems}"
        )
    else:  # pragma: no cover — учётка, названная цифрами uid, на CI не заводится
        assert not any("не разрешается в системе" in p for p in problems), (
            f"пользователь «{uid}» в системе ЕСТЬ, и отказ по разрешению "
            f"имени был бы ложным: {problems}"
        )
    # А у systemd-run голое число uid-ом БЫТЬ ОБЯЗАНО — иначе доделка выше
    # сломала бы неразрешённую 534e16e4, ради которой числовая форма и заведена.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/systemd-run --scope --uid={uid} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    entry = local_reviewer._resolve_user(user, tool)
    assert entry is not None and entry.pw_uid == uid

    # Имя пользователя по-прежнему разрешается по имени, а мусор с решёткой —
    # по-прежнему нет: отказ остаётся отказом.
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        f"/usr/bin/sudo -n -u {me.pw_name} /usr/local/bin/wrap",
    )
    tool, user = local_reviewer._named_sandbox_user()
    assert local_reviewer._resolve_user(user, tool) is not None
    for junk in ("#", "#abc", "#-1"):
        monkeypatch.setattr(
            config,
            "LOCAL_REVIEW_SANDBOX",
            f"/usr/bin/sudo -n -u '{junk}' /usr/local/bin/wrap",
        )
        tool, user = local_reviewer._named_sandbox_user()
        assert local_reviewer._resolve_user(user, tool) is None, (
            f"«{junk}» числовым uid не является, и разрешаться не должен"
        )


def test_the_scratch_check_reaches_the_group_through_a_hash_uid(
    monkeypatch, tmp_path
) -> None:
    """Проверка группы каталога прогонов ДОХОДИТ до существа на форме ``#UID``.

    Отдельный тест, а не хвост предыдущего, по двум причинам. Первая — так
    видно, ЧТО именно ломается: прямые вызовы ``_resolve_user`` рядом падают
    первыми и накрывают собой эту проверку. Вторая важнее: прошлая редакция
    ставила здесь ``LOCAL_REVIEW_SCRATCH_DIR = ""`` и ждала ``[]`` — а
    ``scratch_problem()`` на пустой настройке возвращает ``[]`` ПЕРВОЙ ЖЕ
    СТРОКОЙ, не дойдя ни до ``_uid_outside_group``, ни до ``_resolve_user``.
    Утверждение было верным при любом состоянии кода, то есть не держало
    ничего; комментарий при нём обещал ровно обратное («scratch_problem зовёт
    его сам»). Поэтому каталог здесь настоящий.

    Существо: пользователь РАЗРЕШЁН, значит «не смогли проверить» звучать не
    должно.

    Утверждениями ПОЛОЖИТЕЛЬНЫМИ, а не запретом фразы. Прошлая редакция
    запрещала здесь только слова «не разрешается в системе» — и оставалась
    зелёной, когда извлечение пользователя ломалось совсем: при пустом
    ``user`` ``_uid_outside_group`` возвращает ``[]`` первой же строкой, до
    ``_resolve_user`` дело не доходит, и запрещать в пустом списке нечего.
    Замерено на HEAD 684bab0b: подменить ``_named_sandbox_user`` на
    ``("", "")`` — ``scratch_problem()`` даёт ``[]``, утверждение True
    (неразрешённая ревью #373). Поэтому тест теперь называет и извлечение, и
    ОБА исхода сравнения с группой.
    """
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid())
    uid = me.pw_uid
    scratch = tmp_path / "runs"
    scratch.mkdir()
    # chown ДО chmod: непривилегированный chown снимает setgid.
    os.chown(scratch, -1, me.pw_gid)
    scratch.chmod(0o2770)
    st = os.stat(scratch)
    assert st.st_mode & stat.S_ISGID, (
        "каталог без setgid уводит scratch_problem() в ДРУГУЮ ветку, и тест "
        "снова ничего не проверит — как это уже было с пустой настройкой"
    )
    assert st.st_gid == me.pw_gid, "группа каталога не та, о которой судит тест"

    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(scratch))
    monkeypatch.setattr(
        config, "LOCAL_REVIEW_SANDBOX", f"/usr/bin/sudo -n -u #{uid} /wrap"
    )

    # 1. Пользователь ИЗВЛЁКСЯ. Без этого всё ниже вырождается в проверку
    #    пустого списка, а вырожденный тест хуже отсутствующего: он показывает
    #    покрытие там, где его нет.
    assert local_reviewer._named_sandbox_user() == ("sudo", f"#{uid}"), (
        "форма «-u #UID» не извлеклась из песочницы: "
        f"{local_reviewer._named_sandbox_user()}"
    )

    # 2. Группа СВОЯ — путь доходит до сравнения и разрешает. Пустой список
    #    здесь означает «проверил и допустил», и держит его пункт 1.
    problems = local_reviewer.scratch_problem()
    assert problems == [], (
        f"«sudo -u #{uid}» называет существующего пользователя синтаксисом "
        "самого sudo, каталог принадлежит его же основной группе — а хаб "
        f"всё равно нашёл причину: {problems}"
    )

    # 3. Группа ЧУЖАЯ — тот же путь доходит до сравнения и НАЗЫВАЕТ имена.
    #    Это и есть утверждение, которого не хватало: если ``#UID`` перестанет
    #    разрешаться, здесь встанет «не разрешается в системе», а если
    #    извлечение сломается — пустой список. Оба падают.
    mine = set(os.getgroups()) | {me.pw_gid}
    foreign = next(
        (
            g
            for g in grp.getgrall()
            if g.gr_gid not in mine and me.pw_name not in g.gr_mem
        ),
        None,
    )
    assert foreign is not None, "на машине не нашлось группы, в которой мы не состоим"
    outside = local_reviewer._uid_outside_group(str(scratch), foreign.gr_gid)
    assert len(outside) == 1, (
        f"членство в чужой группе «{foreign.gr_name}» обязано быть названо "
        f"причиной, а страж вернул {outside}"
    )
    assert "не разрешается в системе" not in outside[0], (
        f"форма «#{uid}» снова не разрешилась, и оператору называется ложная "
        f"причина: {outside[0]}"
    )
    assert me.pw_name in outside[0] and foreign.gr_name in outside[0], (
        "отказ обязан назвать И пользователя, И группу — иначе чинить нечего: "
        f"{outside[0]}"
    )


def _doc_limits_probe() -> str:
    """Фрагмент пробы лимитов ИЗ ДОКУМЕНТА — от шага 3 до конца блока.

    Берётся текстом из файла, а не переписывается сюда: переписанная проба
    проверяла бы тест, а не документ.
    """
    blocks = [
        b
        for b in re.findall(r"```bash\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "memory.max" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один bash-блок с пробой "
        f"memory.max, найдено {len(blocks)}"
    )
    lines = blocks[0].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# 3."))
    return "\n".join(lines[start:])


def test_the_doc_limits_probe_tells_failure_from_success(tmp_path) -> None:
    """Проба лимитов ОТЛИЧАЕТ отказ движка от успеха, а не читает молчание.

    Прежняя редакция запускала `haiplane-review:latest` — литерал, который
    абзац «Остальные расхождения» того же документа называет разошедшимся с
    настоящим именем образа (`localhost/haiplane-reviewer:1`). Образа с таким
    именем на хосте нет, podman отказывает и НИЧЕГО не печатает, а критерий
    был написан отрицанием: «не должно быть 'max'». Замерено::

        rc=125, stdout=[] -> слова 'max' нет -> оператор читает УСПЕХ

    То есть проба доказывала лимиты собственным отказом. Здесь она
    ИСПОЛНЯЕТСЯ с подставным движком, и проверяется ровно то, что чинит
    находка: четыре исхода — лимиты есть, лимитов нет, ответа нет, проба не
    запустилась — обязаны звучать ПО-РАЗНОМУ.
    """
    probe = _doc_limits_probe()
    # Судятся ИСПОЛНЯЕМЫЕ строки: комментарий рядом называет старый литерал
    # именно затем, чтобы его сюда не вернули, и запретом на слово это не
    # проверить.
    runnable = "\n".join(
        line for line in probe.splitlines() if not line.lstrip().startswith("#")
    )
    assert "haiplane-review:latest" not in runnable, (
        "проба запускает образ, который этот же документ называет выдуманным "
        "литералом прежней редакции"
    )
    assert "/etc/haiplane-review/image" in runnable, (
        "образ обязан читаться из того же файла, что читает враппер: литерал "
        "в пробе уже расходился с настоящим именем образа"
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sudo").write_text('#!/bin/sh\n[ "$1" = "-u" ] && shift 2\nexec "$@"\n')
    imagefile = tmp_path / "image"
    imagefile.write_text("localhost/haiplane-reviewer:1\n")
    argv_log = tmp_path / "argv.log"

    engines = {
        # Образа нет — ровно исход прежней редакции: rc 125, stdout пуст.
        "отказ движка": '#!/bin/sh\necho "Error: no such image" >&2\nexit 125\n',
        # Движок отработал, но лимитов нет.
        "лимитов нет": "#!/bin/sh\necho max\n",
        # Движок отработал и не напечатал ничего — то же молчание, что у
        # отказа, но с нулевым кодом.
        "пустой вывод": "#!/bin/sh\nexit 0\n",
        # Лимиты применены.
        "лимиты есть": "#!/bin/sh\necho 1572864000\n",
    }
    verdicts: dict[str, str] = {}
    for label, body in engines.items():
        podman = bindir / "podman"
        podman.write_text(
            body.replace(
                "#!/bin/sh\n",
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ARGV_LOG"\n',
                1,
            )
        )
        for path in (bindir / "sudo", podman):
            path.chmod(0o755)
        done = subprocess.run(
            [
                "/bin/sh",
                "-c",
                probe.replace("/etc/haiplane-review/image", str(imagefile)),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "ARGV_LOG": str(argv_log),
            },
        )
        printed = done.stdout.strip()
        assert printed, (
            f"на исходе «{label}» проба не сказала НИЧЕГО: оператору нечего "
            f"прочитать (stderr: {done.stderr!r})"
        )
        # Приговор — то, что стоит ДО двоеточия. Сравнивать целые строки
        # значило бы различать исходы по подставленному числу: мутация,
        # печатающая на отказе движка «лимиты применены: memory.max=» с
        # пустым значением, отличалась бы от успеха одним лишь числом и
        # ВЫЖИЛА (замерено в мутационной серии, M2).
        verdicts[label] = printed.split(":", 1)[0].strip()

    assert len(set(verdicts.values())) == len(verdicts), (
        "исходы пробы неразличимы по приговору: оператор не сможет отличить "
        f"отказ движка от применённых лимитов, читая одно и то же — {verdicts}"
    )

    # И образ — тот, что лежит в файле, а не зашитый в пробу литерал.
    seen = argv_log.read_text()
    assert "localhost/haiplane-reviewer:1" in seen, (
        f"проба запустила не тот образ, что назван в файле: {seen!r}"
    )


def _doc_table(heading: str) -> list[list[str]]:
    """Строки таблицы под НАЗВАННЫМ заголовком документа, ячейками.

    Заголовок обязателен: таблиц в документе несколько, и брать «все строки,
    начинающиеся с | `» значило бы судить таблицу принятых форм правилами
    таблицы отказов. Пустая выборка — провал теста, а не зелёный прогон: она
    была бы зелёной при любом содержании документа.
    """
    lines = _DEPLOY_DOC.read_text().splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == heading.strip()), -1
    )
    assert start >= 0, f"в {_DEPLOY_DOC.name} нет заголовка «{heading}»"
    rows: list[list[str]] = []
    for line in lines[start:]:
        if line.startswith("## ") or line.startswith("### "):
            if rows:
                break
            continue
        if line.startswith("| `"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    assert rows, f"под «{heading}» не нашлось таблицы — проверка была бы пустой"
    return rows


def _backticked(cell: str) -> str:
    assert cell.count("`") >= 2, f"в ячейке «{cell}» нет строки в обратных кавычках"
    return cell.split("`")[1]


def test_the_doc_names_the_closed_set_the_guard_accepts(monkeypatch) -> None:
    """AC-5/AC-6: набор форм в документе — это набор форм В КОДЕ, сверено машиной.

    Расхождение документа с кодом на этой задаче случалось ПЯТЬ раз и глазами
    не поймалось ни разу: документ рекомендовал `systemd-run --uid=`, который
    на проде не стартует; советовал `--timeout` «не меньше» хабского, тогда
    как страж отвергает «больше»; обещал, что незнакомое пропускается, тогда
    как незнакомый флаг давал отказ. Поэтому сверяется не глазами: имена форм,
    строки целиком и ОБА списка флагов берутся из таблицы документа и
    сравниваются с ``SANDBOX_SHAPES``, а каждая строка прогоняется через
    самого стража.
    """
    rows = _doc_table("### Что хаб принимает")
    shapes = {shape.name: shape for shape in local_reviewer.SANDBOX_SHAPES}
    documented = {_backticked(row[0]) for row in rows}
    assert documented == set(shapes), (
        f"документ называет формы {sorted(documented)}, а код принимает "
        f"{sorted(shapes)}. Форма, которой нет в одном из двух мест, — это "
        "либо обещание без кода, либо код без обещания"
    )

    for row in rows:
        shape = shapes[_backticked(row[0])]
        example = _backticked(row[1])
        assert example == shape.example, (
            f"документ печатает для формы «{shape.name}» строку «{example}», а "
            f"код держит «{shape.example}». Оператор скопирует первую"
        )
        required = {
            flag.strip()
            for cell in row[2].split(",")
            for flag in cell.replace("`", " ").split("/")
            if flag.strip().startswith("-")
        }
        assert required == {flag for group in shape.required for flag in group}, (
            f"обязательные флаги формы «{shape.name}»: документ называет "
            f"{sorted(required)}, код требует "
            f"{sorted(flag for group in shape.required for flag in group)}"
        )
        optional = {
            flag.strip()
            for flag in row[3].replace("`", " ").split(",")
            if flag.strip().startswith("-")
        }
        in_code = (shape.valueless | shape.valued) - {
            flag for group in shape.required for flag in group
        }
        assert optional == in_code, (
            f"необязательные флаги формы «{shape.name}»: документ называет "
            f"{sorted(optional)}, код принимает {sorted(in_code)}. Флаг, "
            "названный в документе и не принятый кодом, — это отказ на строке, "
            "которую документ рекомендует"
        )

        # И то же самое ИСПОЛНЕНИЕМ: строка из документа прогоняется через
        # стража и обязана подойти именно той форме, которой её назвали.
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", example)
        assert local_reviewer.sandbox_problem() == [], (
            f"документ печатает «{example}» как форму «{shape.name}», а хаб её "
            f"отвергает: {local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_shape() == shape.name


def test_the_doc_table_of_refusals_is_what_the_guard_actually_refuses(
    monkeypatch,
) -> None:
    """AC-4, обратная сторона: строки из таблицы ОТКАЗОВ хаб и вправду отвергает.

    Пара к test_the_doc_recommends_only_sandboxes_the_hub_accepts. Документ,
    обещающий отказ там, где хаб молча пропускает, готовит ровно тот выкат, из
    которого выросла задача. Строки берутся из таблицы, а не переписываются
    сюда.

    В таблице стоят НАСТОЯЩИЕ входы, на которых страж когда-то ошибался, —
    включая те, что прежняя редакция документа обещала ПРИНИМАТЬ (`podman run
    --timeout 1800`). Смена правила не выбросила ни одного из них: они
    отвергаются тем более, и документ обязан говорить об этом ровно то же,
    что делает код.
    """
    refused = [_backticked(row[0]) for row in _doc_table("### Что хаб отвергает")]
    assert len(refused) >= 10, (
        f"таблица отказов слишком коротка ({len(refused)}): накопленные за "
        "девять кругов входы обязаны стоять в документе, а не только в тестах"
    )
    # Суждение о сроке зависело от хабского, и брать его из окружения значило
    # бы судить документ по чужой переменной. Приговор от него больше не
    # зависит, но число в документе и в примере окружения обязано быть одно —
    # это и проверяет _documented_hub_deadline.
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", _documented_hub_deadline())

    for sandbox in refused:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        reasons = local_reviewer.sandbox_problem()
        assert reasons, (
            f"документ обещает отказ на «{sandbox}», а хаб пропускает её "
            "молча: обещание в тексте, которого нет в коде, — это тот же "
            "выкат, ради которого писан весь документ"
        )
        assert local_reviewer.sandbox_uid() == "", (
            f"на отвергнутой «{sandbox}» страж назвал пользователя "
            f"«{local_reviewer.sandbox_uid()}»"
        )

    # Классы, за которыми документ обязан следить поимённо: контейнерный
    # запуск (отказ называет недостающий --timeout), отсоединённый запуск и
    # срок ДЛИННЕЕ хабского — все три страж когда-то пропускал молча.
    assert any("podman" in s and "--timeout" in s for s in refused), (
        "контейнерный запуск со сроком жизни в таблице не назван, а прежняя "
        "редакция документа его РЕКОМЕНДОВАЛА"
    )
    assert any("-d" in s or "--detach" in s for s in refused), (
        "отсоединённый запуск в таблице отказов не назван, а он проходил "
        "стража молча (воспроизведено на 5b7b813)"
    )
    assert any("3600" in s for s in refused), (
        "срок контейнера ДЛИННЕЕ хабского в таблице отказов не назван, а "
        "документ до 10.09.2026 такой срок прямо СОВЕТОВАЛ («не меньше»)"
    )
    assert any("-p80:80" in s for s in refused), (
        "строка с неизвестным флагом при ЖИВОМ сроке в таблице не названа, а "
        "именно на ней текст документа расходился с кодом"
    )


def test_the_doc_recommends_only_sandboxes_the_hub_accepts(monkeypatch) -> None:
    """AC-4: строки песочницы ИЗ ДОКУМЕНТА прогоняются через самих стражей.

    Документ, рекомендующий то, что хаб сам же отклонит, хуже отсутствующего.
    Строки берутся из файлов, а не переписываются сюда: переписанная строка
    проверяла бы тест, а не документ.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    # Значение — всё после первого '<ИМЯ>=' в строке.
    recommended = [
        line.split(key + "=", 1)[1]
        for path in (_DEPLOY_DOC, _ENV_EXAMPLE)
        for line in path.read_text().splitlines()
        if key + "=" in line
    ]
    assert len(recommended) >= 2, (
        "в документе и примере окружения не нашлось рекомендованных строк "
        f"{key}= — проверка была бы пустой и зелёной при любом их содержании"
    )

    for sandbox in recommended:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        assert local_reviewer.sandbox_problem() == [], (
            f"документ рекомендует «{sandbox}», а хаб её отвергает: "
            f"{local_reviewer.sandbox_problem()}"
        )
        assert local_reviewer.sandbox_uid(), (
            f"в рекомендованной строке «{sandbox}» страж не видит "
            "пользователя — значит проверка его членства в группе каталога "
            "прогонов на этой конфигурации молча не сработает вовсе, ровно "
            "как было до #1208"
        )


def test_the_doc_wrapper_carries_the_argv_the_hub_appends(
    monkeypatch, tmp_path
) -> None:
    """Скелет враппера ИЗ ДОКУМЕНТА доносит до движка argv, дописанный хабом.

    Хаб запускает ``shlex.split(SANDBOX) + shlex.split(CMD)``. Враппер без
    ``"$@"`` этот хвост молча выбрасывает: измерено на прежней редакции
    скелета — ``haiplane-review-run cursor-agent --print`` доходило до podman
    строкой БЕЗ ``cursor-agent``, код возврата 0, ни ошибки, ни следа.
    Запускалось бы то, что зашито в образ, а не то, что стоит в настройке —
    тот же класс отказа, который чинила задача: не ломает, а тихо подменяет
    (найдено машинным ревью 09.09.2026, находка 8d363dc407782056).

    Скелет берётся ИЗ ФАЙЛА и ИСПОЛНЯЕТСЯ, а не читается глазами: переписанный
    в тест, он проверял бы тест, а прочитанный — ничего.
    """
    # 1. Хаб действительно дописывает CMD к SANDBOX — это наблюдение, а не
    #    посылка: на ней держится всё остальное в этом тесте.
    monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", "/usr/bin/sudo -n -u r /wrap")
    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "cursor-agent --print")
    assert local_reviewer.argv() == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "r",
        "/wrap",
        "cursor-agent",
        "--print",
    ], "хаб склеивает песочницу и CMD — если это не так, весь тест ниже мимо"

    # 2. Скелет враппера из документа, с подменённым движком на заглушку,
    #    печатающую свой argv.
    blocks = [
        b
        for b in re.findall(r"```sh\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "podman run" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один sh-скелет враппера с "
        f"podman run, найдено {len(blocks)}"
    )
    script = textwrap.dedent(blocks[0])
    engine = next(
        tok
        for tok in shlex.split(script.replace("\\\n", " "))
        if tok.rsplit("/", 1)[-1] == "podman"
    )
    stub = tmp_path / "podman"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    stub.chmod(0o755)
    wrapper = tmp_path / "haiplane-review-run"
    wrapper.write_text(script.replace(engine, str(stub)))
    wrapper.chmod(0o755)

    cmd = ["cursor-agent", "--print", "--model", "grok-4.6"]
    done = subprocess.run(
        [str(wrapper), *cmd],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "HAIPLANE_REVIEW_CONF": str(_wrapper_conf(tmp_path, deadline=None)),
        },
    )
    assert done.returncode == 0, f"скелет враппера не запустился: {done.stderr}"
    seen = done.stdout.split("\n")
    assert seen[-1 - len(cmd) : -1] == cmd, (
        "скелет враппера из документа НЕ донёс до движка argv, который хаб к "
        f"нему дописал: движок получил {seen[:-1]}, а хвостом обязан был "
        f'стоять {cmd}. Молча: код возврата 0. Добавьте "$@" последним '
        "аргументом podman run"
    )


def test_the_doc_probe_runs_the_same_command_the_hub_will_run(monkeypatch) -> None:
    """Проба песочницы в документе гоняет тот же argv, что и живой запуск.

    Проба, которая запускает враппер голым, проходит и на скелете, молча
    выбрасывающем argv, — то есть доказывает не то, что проверяет.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    text = _DEPLOY_DOC.read_text()
    recommended = [
        line.split(key + "=", 1)[1] for line in text.splitlines() if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="
    wrapper = shlex.split(recommended[0])[-1]

    probes = [
        line
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        for line in block.splitlines()
        if wrapper in line
    ]
    assert probes, (
        f"в {_DEPLOY_DOC.name} нет пробы, запускающей враппер {wrapper} — "
        "проверка была бы пустой и зелёной при любом её содержании"
    )
    for line in probes:
        assert "$CMD" in line, (
            f"проба «{line.strip()}» запускает враппер БЕЗ дописанного argv "
            "CLI, а хаб запускает песочницу вместе с ним "
            f"({key[:-7]}CMD). Такая проба пройдёт там, где живой запуск "
            "пойдёт другой командой"
        )


# Флаги ``podman run``, которые встречаются в СКЕЛЕТЕ ВРАППЕРА из документа.
# Список живёт здесь, а не в хабе: с 10.09.2026 хаб строку podman не разбирает
# вовсе — контейнерного запуска нет в закрытом наборе форм песочницы (#1208),
# а внутрь враппера он не заглядывает по определению. Тесты документа читают
# скелет сами, и незнакомый флаг для них — ПРОВАЛ разбора, о котором тест
# кричит, а не угадывает: иначе проверка «--timeout доехал» была бы зелёной на
# строке, которую тест не понял.
_WRAPPER_VALUE_FLAGS = frozenset(
    {
        "--cpus",
        "--env-file",
        "-m",
        "--memory",
        "--name",
        "--net",
        "--network",
        "--pids-limit",
        "--timeout",
        "--userns",
        "-v",
        "--volume",
    }
)
_WRAPPER_VALUELESS_FLAGS = frozenset(
    {"--init", "-i", "-it", "--read-only", "--replace", "--rm", "-t"}
)


def _wrapper_run_flags(parts: list[str]) -> list[tuple[str, str | None]]:
    """Флаги самого ``podman run`` из скелета враппера — ДО образа.

    Всё, что стоит после образа, — команда внутри контейнера, и её флаги к
    запуску отношения не имеют.
    """
    assert "run" in parts, f"в строке запуска нет подкоманды run: {parts}"
    i = parts.index("run")
    assert parts[i - 1].rsplit("/", 1)[-1] in ("podman", "docker"), (
        f"«run» стоит не за движком: {parts}"
    )
    flags: list[tuple[str, str | None]] = []
    k = i + 1
    while k < len(parts) and parts[k].startswith("-"):
        name, sep, value = parts[k].partition("=")
        if sep:
            flags.append((name, value))
            k += 1
        elif name in _WRAPPER_VALUE_FLAGS:
            flags.append((name, parts[k + 1] if k + 1 < len(parts) else None))
            k += 2
        else:
            assert name in _WRAPPER_VALUELESS_FLAGS, (
                f"скелет враппера содержит флаг «{name}», о котором этот тест "
                f"не знает, берёт ли тот значение отдельным токеном: {parts}. "
                "Разбор недостоверен, и судить по нему нельзя"
            )
            flags.append((name, None))
            k += 1
    return flags


def _wrapper_flag(flags: list[tuple[str, str | None]], flag: str) -> str | None:
    """ДЕЙСТВУЮЩЕЕ значение флага — последнее вхождение, как берёт его pflag."""
    values = [value for name, value in flags if name == flag]
    return values[-1] if values else None


def _wrapper_conf(tmp_path, *, deadline: int | None) -> Path:
    """Каталог ``$CONF`` враппера: то, что оператор кладёт в /etc по шагу 3a.

    Скелет читает оттуда образ и срок — сам он их не зашивает, иначе срок ни
    за какой настройкой не следовал бы (находка 10.09.2026). Файл ``timeout``
    при ``deadline is None`` НЕ создаётся: это вход «оператор файла не писал»,
    на котором действует умолчание скелета.
    """
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    (conf / "image").write_text("localhost/haiplane-reviewer:1\n")
    (conf / "model.env").write_text("KEY=x\n")
    if deadline is None:
        (conf / "timeout").unlink(missing_ok=True)
    else:
        (conf / "timeout").write_text(f"{deadline}\n")
    return conf


def _doc_wrapper_argv(tmp_path, *, deadline: int | None) -> list[str]:
    """ВСЕ вызовы движка, которые сделал скелет враппера из документа.

    Скелет не читается глазами и не разбирается регулярками: он ИСПОЛНЯЕТСЯ с
    подменённым на заглушку движком, и наружу отдаётся то, что заглушка
    получила — каждый вызов отдельной строкой, в порядке вызова. Разница не
    косметическая — в скелете стоят переменные (``--timeout="$TIMEOUT"``,
    ``"$IMAGE"``), и текст «--timeout "$TIMEOUT"» выглядит убедительно ровно
    так же при пустом TIMEOUT, при опечатке в имени переменной и при её потере
    под ``set -u``. Проверять текст значило бы проверять намерение, а не
    команду.

    ``deadline`` — что лежит в ``$CONF/timeout``; ``None`` значит файла нет, и
    тогда действует умолчание самого скелета. Каталог настроек подставляется
    через ``HAIPLANE_REVIEW_CONF``: без него скелет читал бы боевой
    ``/etc/haiplane-review``, которого на машине проверки нет, а с зашитым в
    скелет литералом срок вообще не следовал бы ни за чем (находка 10.09.2026,
    находки 2 и 3 одного и того же числа).

    Возвращаются ВСЕ вызовы, а не только ``run``: то, что скелет делает ДО
    запуска, — часть рецепта, и именно там жил ``podman rm -af``, сносивший
    чужие контейнеры.
    """
    conf = _wrapper_conf(tmp_path, deadline=deadline)
    blocks = [
        b
        for b in re.findall(r"```sh\n(.*?)```", _DEPLOY_DOC.read_text(), re.S)
        if "podman run" in b
    ]
    assert len(blocks) == 1, (
        f"в {_DEPLOY_DOC.name} ожидался ровно один sh-скелет враппера с "
        f"podman run, найдено {len(blocks)}"
    )
    script = textwrap.dedent(blocks[0])
    engine = next(
        tok
        for tok in shlex.split(script.replace("\\\n", " "))
        if tok.rsplit("/", 1)[-1] == "podman"
    )
    # Свой файл на КАЖДЫЙ прогон: заглушка дописывает, и общий файл склеил бы
    # вызовы соседнего запуска скелета в том же tmp_path.
    log = tmp_path / f"argv-{uuid.uuid4().hex}.log"
    stub = tmp_path / "podman"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shlex, sys\n"
        "open(os.environ['ARGV_LOG'], 'a').write(shlex.join(sys.argv[1:]) + '\\n')\n"
    )
    stub.chmod(0o755)
    wrapper = tmp_path / "haiplane-review-run"
    wrapper.write_text(script.replace(engine, str(stub)))
    wrapper.chmod(0o755)

    done = subprocess.run(
        [str(wrapper), "cursor-agent", "--print"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "ARGV_LOG": str(log),
            "HAIPLANE_REVIEW_CONF": str(conf),
        },
    )
    assert done.returncode == 0, f"скелет враппера не запустился: {done.stderr}"
    seen = [line for line in log.read_text().splitlines() if line]
    log.unlink()
    return [shlex.join([engine, *shlex.split(line)]) for line in seen]


def _doc_wrapper_launch(tmp_path, *, deadline: int | None = None) -> str:
    """Единственная строка ``<engine> run`` скелета — то, что судит страж."""
    calls = _doc_wrapper_argv(tmp_path, deadline=deadline)
    runs = [c for c in calls if shlex.split(c)[1:2] == ["run"]]
    assert len(runs) == 1, (
        f"скелет враппера обязан один раз позвать «podman run»; движок получил {calls}"
    )
    return runs[0]


def test_the_doc_wrapper_skeleton_names_its_own_deadline(monkeypatch, tmp_path) -> None:
    """Срок жизни контейнера в скелете враппера держится тестом, а не текстом.

    Рекомендованная песочница — ``sudo … /usr/local/bin/haiplane-review-run``,
    и стражам хаба она непрозрачна: podman лежит ВНУТРИ скрипта. Значит на
    рекомендованном рецепте единственное место, где срок жизни вообще
    существует, — это скелет враппера в документе. Замер: вычеркнуть из него
    ``--timeout 1800`` — и весь набор оставался зелёным, включая AC-1..AC-4,
    тест argv и тест пробы (09.09.2026, находка машинного ревью
    b3a35600e66e971d). То есть флаг, ради которого писан страж, на
    рекомендованном пути не держался ничем.

    Проверяется не глазами и не подстрокой: строка запуска берётся ИЗ ФАЙЛА и
    ИСПОЛНЯЕТСЯ (переменные скелета подставляются оболочкой, а не читателем).

    ПОВОРОТ 10.09.2026. Прежде эту строку прогоняли через самого стража хаба.
    Теперь так нельзя, и не потому, что стало хуже: контейнерный запуск в
    закрытый набор форм песочницы не входит, и страж отверг бы ЛЮБУЮ строку
    podman — в том числе верную. Судить скелет ему больше нечем и незачем: он
    и на рекомендованном рецепте его не видел. Поэтому срок берётся из
    исполненного скелета и сравнивается с ХАБСКИМ СРОКОМ ИЗ ДОКУМЕНТА — с тем
    самым числом, которое оператор скопирует к себе.
    """
    launch = _doc_wrapper_launch(tmp_path)
    # Хвост ``cursor-agent --print`` — команда ВНУТРИ контейнера; флаги
    # запуска кончаются на образе, и до хвоста разбор не доходит.
    flags = _wrapper_run_flags(shlex.split(launch))
    lifetime = _wrapper_flag(flags, "--timeout")
    assert lifetime and lifetime.isdigit() and int(lifetime) > 0, (
        f"скелет враппера из документа запускает «{launch}» без собственного "
        "срока жизни. Контейнер переживёт снятие прогона — измерено "
        "08.09.2026: kill -KILL по группе, через 7 с контейнер «Up», — а хаб "
        "напишет в ленту, что снял"
    )
    # Срок контейнера не может быть БОЛЬШЕ хабского: он истёк бы уже после
    # того, как хаб закрыл прогон. Хабский берётся из документа, а не из
    # окружения, иначе скелет судился бы чужой переменной.
    hub_deadline = _documented_hub_deadline()
    assert int(lifetime) <= hub_deadline, (
        f"скелет даёт контейнеру {lifetime} с при хабских {hub_deadline}: "
        "контейнер переживёт снятие прогона ровно так же, как без --timeout "
        "вовсе, только тише"
    )


def test_the_doc_wrapper_deadline_follows_the_conf_file(monkeypatch, tmp_path) -> None:
    """Срок контейнера СЛЕДУЕТ за настройкой, а не зашит в скелет числом.

    Находка 10.09.2026 (ревьюер Codex, воспроизведено на ef2198fc): оператор
    ставит ``HAIPLANE_LOCAL_REVIEW_TIMEOUT_SEC=600`` и копирует скелет как
    есть — а в скелете стояло ``TIMEOUT=1800`` литералом, ни с чем не
    связанным. Контейнер переживал прогон втрое, и страж этого не видел вовсе:
    на рекомендованном рецепте ему виден только ``sudo …
    /haiplane-review-run``, podman лежит внутри скрипта.

    Проверяется исполнением: скелет запускается с подставленным каталогом
    настроек, и наружу берётся то число, которое ПОЛУЧИЛ движок. Замер на
    прежней редакции: файл ``timeout`` с любым содержимым не менял ничего —
    движку уходило 1800.
    """
    hub_deadline = _documented_hub_deadline()

    # 1. Умолчание скелета (файла нет) — это ХАБСКОЕ умолчание, а не «просто
    #    число»: разойдись они, рецепт по умолчанию был бы уже сломан.
    launch = _doc_wrapper_launch(tmp_path, deadline=None)
    flags = _wrapper_run_flags(shlex.split(launch))
    assert _wrapper_flag(flags, "--timeout") == str(hub_deadline), (
        f"без файла настроек скелет ставит контейнеру срок из «{launch}», а "
        f"хабское умолчание — {hub_deadline}. Умолчания обязаны совпадать: "
        "иначе рецепт ломается ещё до того, как оператор что-либо тронул"
    )

    # 2. Файл настроек ДЕЙСТВУЕТ: оператор укоротил хабский срок, вписал то же
    #    число в файл — и контейнер получил именно его. Это и есть то, чего в
    #    прежней редакции не было: связь между двумя местами.
    shortened = 600
    assert shortened != hub_deadline, "проверка беспредметна на равных числах"
    launch = _doc_wrapper_launch(tmp_path, deadline=shortened)
    flags = _wrapper_run_flags(shlex.split(launch))
    assert _wrapper_flag(flags, "--timeout") == str(shortened), (
        f"скелет не взял срок из $CONF/timeout: движку ушло «{launch}». "
        "Значит число в скелете зашито, и согласовать его с настройкой хаба "
        "оператору нечем"
    )
    # ПОВОРОТ 10.09.2026: прежде здесь стояла третья проверка — что строку
    # запуска враппера принимает сам страж хаба. Она потеряла предмет:
    # контейнерный запуск в закрытый набор форм песочницы не входит, и хаб
    # эту строку не увидит вовсе — ему видна только форма sudo, а podman
    # лежит внутри скрипта. Согласие двух чисел от этого не ослабло: оно и
    # есть то, что проверено выше, — и проверено ИСПОЛНЕНИЕМ скелета.
    monkeypatch.setattr(config, "LOCAL_REVIEW_TIMEOUT_SEC", shortened)
    monkeypatch.setattr(
        config,
        "LOCAL_REVIEW_SANDBOX",
        "/usr/bin/sudo -n -u haiplane-reviewer /usr/local/bin/haiplane-review-run",
    )
    assert local_reviewer.sandbox_problem() == [], (
        "хаб видит не строку podman, а форму sudo, и она обязана проходить "
        f"при любом сроке: {local_reviewer.sandbox_problem()}"
    )

    # 3. И РАСХОЖДЕНИЕ — это дыра, а не мелочь: не тронув файл, оператор
    #    получит на укороченном хабе контейнер с прежним умолчанием. Страж
    #    этого не увидит НИКОГДА — ни прежде, ни теперь: на рекомендованном
    #    рецепте podman лежит внутри скрипта. Поэтому файл из шага 3a и есть
    #    единственное место, где расхождение закрывается, и здесь измеряется
    #    именно оно.
    stale = _wrapper_flag(
        _wrapper_run_flags(shlex.split(_doc_wrapper_launch(tmp_path))), "--timeout"
    )
    assert stale == str(hub_deadline) and int(stale) > shortened, (
        f"без файла скелет даёт контейнеру {stale} с — и на хабских "
        f"{shortened} это переживший прогон контейнер. Если бы умолчание "
        "скелета следовало за настройкой само, шаг 3a был бы не нужен вовсе"
    )


def test_the_doc_wrapper_cleans_up_only_its_own_container(tmp_path) -> None:
    """Уборка хвоста адресована ОДНОМУ контейнеру, а не всем подряд.

    Находка 10.09.2026 (ревьюер Codex, подтверждена на хосте хаба:
    /usr/local/bin/haiplane-review-run, строка 22). Скелет содержал
    ``podman rm -af`` — снос ВСЕХ контейнеров пользователя-ревьюера.
    Основание было названо тут же в комментарии («два ревьюера разом хосту не
    по карману»), но ``rm -af`` такого ограничения не устанавливает: он не
    сдерживает второй прогон, а УБИВАЕТ первый — и заодно всё постороннее,
    что этот пользователь запустил. Одновременность держит хаб
    (``local_reviewer._HOST_BUDGET``, тест
    ``test_two_local_runs_never_overlap_on_the_host``), а здесь держится то,
    что уборка никого чужого не задевает.

    Судится не текст, а argv, которые движок ФАКТИЧЕСКИ получил.
    """
    calls = _doc_wrapper_argv(tmp_path, deadline=None)
    # ``--all``/``-a`` у любой снимающей подкоманды — это «все контейнеры
    # пользователя», то есть ровно та находка. Перечислены по имени: список
    # узкий и о подкомандах podman, а не догадка про CLI вообще.
    for call in calls:
        argv = shlex.split(call)[1:]
        if not argv or argv[0] not in ("rm", "stop", "kill", "pod"):
            continue
        wholesale = [
            tok
            for tok in argv[1:]
            if tok in ("-a", "--all")
            or (tok.startswith("-") and not tok.startswith("--") and "a" in tok[1:])
        ]
        assert not wholesale, (
            f"скелет враппера зовёт «{call}»: {wholesale} значит ВСЕ "
            "контейнеры пользователя-ревьюера, а не хвост своего прошлого "
            "прогона. Так уборка сносит живое чужое ревью, и в карточке это "
            "ляжет отказом прогона с ложной причиной"
        )
    # А адресат уборки обязан БЫТЬ: снести хвост всё-таки надо, и адресуется
    # он именем. Без имени ``--replace`` бессмыслен, а без ``--replace``
    # хвост пережил бы запуск и занял бы имя.
    launch = _doc_wrapper_launch(tmp_path, deadline=None)
    flags = _wrapper_run_flags(shlex.split(launch))
    name = _wrapper_flag(flags, "--name")
    assert isinstance(name, str) and name, (
        f"скелет не даёт контейнеру имени: {launch}. Тогда адресной уборки "
        "нет вовсе, и вернуться к «rm -af» — вопрос одного коммита"
    )
    assert any(flag == "--replace" for flag, _ in flags), (
        f"имя «{name}» есть, а --replace нет: {launch}. Хвост прошлого "
        "прогона займёт имя, и запуск упадёт «name already in use»"
    )


def test_the_doc_wrapper_leaves_the_network_the_hub_hands_the_run(tmp_path) -> None:
    """Скелет враппера НЕ отрезает контейнеру сеть, которой хаб его снабжает.

    Прежняя редакция документа рекомендовала ``--network none`` — четвёртый
    экземпляр того же дефекта, ради которого заведена #1208: документ учил
    настройке, на которой выкат останавливается. Настоящий враппер на хосте
    хаба флага ``--network`` не имеет вовсе (снят 10.09.2026).

    Требование не выдумано и не переписано в тест словами: оно берётся у
    САМОГО ХАБА. ``review_dispatch._delivery_block`` — та функция, что
    собирает промт, — кладёт в него ``curl`` на публичный адрес установки:
    обменять одноразовый код, прочитать постановку, СДАТЬ ОТЧЁТ. Контейнер
    без сети до этого адреса не дойдёт, и остаётся только запасной путь —
    блок в тексте (#1036), который хаб помечает как более слабый. Измерено на
    хосте хаба 10.09.2026 тем же образом: с сетью по умолчанию ``getent
    hosts`` внутри контейнера отвечает про agenthai.ru и api.cursor.com
    (rc 0), с ``--network none`` — rc 2 и ни одной строки.
    """
    hub_base = "https://hub.example"
    delivery = _delivery_block(1208, "CODE-1", hub_base)
    assert hub_base in delivery and "curl" in delivery, (
        "хаб перестал давать прогону сетевой адрес — тогда и требование "
        f"ниже беспредметно, и этот тест надо переписать: {delivery!r}"
    )

    launch = shlex.split(_doc_wrapper_launch(tmp_path))
    # Всё, что стоит ДО образа, — флаги запуска; сеть настраивается только там.
    flags = _wrapper_run_flags(launch)
    for name, value in flags:
        assert name not in ("--network", "--net"), (
            f"скелет враппера из документа задаёт «{name} {value}». Сеть "
            "контейнеру нужна В ДВЕ стороны: к поставщику (agentский CLI "
            "авторизуется ключом из --env-file и ходит в api.cursor.com) и К "
            "САМОМУ ХАБУ — отчёт по контракту сдаётся обычным HTTP на адрес "
            f"из промта ({hub_base} в примере выше). Отрезав её, выкат "
            "остановится молча: прогон кончится без отчёта. Если сеть надо "
            "сузить, сужайте до СПИСКА АДРЕСОВ, а не до нуля"
        )


def test_the_doc_probe_passes_the_cli_argv_as_arguments(monkeypatch) -> None:
    """В пробе ``$CMD`` — ПОЗИЦИОННЫЙ аргумент враппера, а не что попало.

    Прежняя проверка требовала лишь подстроки ``$CMD`` в строке. Замер: строка
    ``CMD=$CMD /usr/local/bin/haiplane-review-run`` — то есть враппер запущен
    ГОЛЫМ, ровно та регрессия, ради которой тест писан, — оставляла весь набор
    зелёным (09.09.2026, находка машинного ревью dc051e23e80f3cdd). Подстрока
    не отличает аргумент от присваивания перед командой и от ``<<<"$CMD"``.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    text = _DEPLOY_DOC.read_text()
    recommended = [
        line.split(key + "=", 1)[1] for line in text.splitlines() if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="
    wrapper = shlex.split(recommended[0])[-1]

    probes = [
        line
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        # Продолжения строк склеиваются: проба разнесена по строкам обратным
        # слешем, и разорванную строку токенами не разобрать.
        for line in block.replace("\\\n", " ").splitlines()
        if wrapper in line
    ]
    assert probes, (
        f"в {_DEPLOY_DOC.name} нет пробы, запускающей враппер {wrapper} — "
        "проверка была бы пустой и зелёной при любом её содержании"
    )
    for line in probes:
        tokens = shlex.split(line, comments=True)
        assert wrapper in tokens, (
            f"в пробе «{line.strip()}» {wrapper} — не отдельное слово команды: "
            "разобрать такую пробу нельзя, и что она запускает, неизвестно"
        )
        tail = tokens[tokens.index(wrapper) + 1 :]
        assert "$CMD" in tail, (
            f"проба «{line.strip()}» запускает враппер БЕЗ дописанного argv "
            f"CLI ПОЗИЦИОННЫМ аргументом, а хаб запускает песочницу именно "
            f"так ({key[:-7]}CMD дописывается хвостом argv). Присваивание "
            "перед командой или подача через heredoc сюда не годятся: "
            "враппер получит пустой argv, и проба пройдёт там, где живой "
            "запуск пойдёт другой командой"
        )


def test_the_recommended_sudo_recipe_still_checks_the_scratch_group(
    monkeypatch, tmp_path
) -> None:
    """Рецепт ИЗ ДОКУМЕНТА прогоняется через not_ready(), а не через страж.

    AC-2 проверяет sandbox_uid() прямым вызовом — и этого мало: решение о
    запуске принимает not_ready(), а его единственная интеграционная проверка
    кормили формой ``systemd-run --uid=``. Замер: вернуть в _uid_outside_group
    разбор ТОЛЬКО ``--uid`` — sandbox_uid() на sudo остаётся верным, AC-2 и
    AC-4 зелёные, весь набор зелёный (rc=0), а not_ready() на рецепте из
    документа возвращает [] и прогон упирается в EACCES внутри чужого
    процесса, где причину уже никто не назовёт (09.09.2026, находка машинного
    ревью 2bdf3c910e8d581c). Ровно то же расхождение, что и на #1155: дверь
    заперта по прямому вызову и открыта по дороге, которой ходят.

    Форма берётся ИЗ ДОКУМЕНТА; подменяется только имя пользователя — оно на
    машине проверки не заведено, а проверяется здесь не оно, а то, что
    проверка вообще СРАБАТЫВАЕТ на рекомендованной форме записи.
    """
    key = config.brand.ENV_PREFIX + "LOCAL_REVIEW_SANDBOX"
    recommended = [
        line.split(key + "=", 1)[1]
        for line in _DEPLOY_DOC.read_text().splitlines()
        if key + "=" in line
    ]
    assert recommended, f"в {_DEPLOY_DOC.name} нет рекомендованной строки {key}="

    import os

    monkeypatch.setattr(config, "LOCAL_REVIEW_CMD", "/bin/true")
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", "token")
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    monkeypatch.setattr(config, "LOCAL_REVIEW_SCRATCH_DIR", str(shared))

    for sandbox in recommended:
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", sandbox)
        named = local_reviewer.sandbox_uid()
        assert named, (
            f"в рекомендованной строке «{sandbox}» страж не видит пользователя"
        )
        # «nobody» есть на обеих системах, где это гоняется, и в группе
        # каталога не состоит — как и в соседней проверке 45971e09.
        probe = sandbox.replace(named, "nobody")
        monkeypatch.setattr(config, "LOCAL_REVIEW_SANDBOX", probe)
        assert any("не состоит в группе" in r for r in local_reviewer.not_ready()), (
            f"на рекомендованной форме «{probe}» not_ready() НЕ назвал "
            "проблему с группой каталога прогонов: значит проверка, заведённая "
            "ради 45971e09, на рецепте из документа молча не срабатывает — "
            f"ровно как было до #1208. Вернулось: {local_reviewer.not_ready()}"
        )
